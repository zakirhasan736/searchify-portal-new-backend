"""Clear, actionable errors and warnings for every report area.

Every failure or caveat the UI shows becomes a *problem*:

    {
      "code": "gsc_property_no_access",
      "area": "search_console",          # keywords | backlinks | ai_visibility | site_audit |
                                         # search_console | analytics | site_scan | google
      "severity": "error",               # error | warning | info
      "title": "Google can't read this Search Console property",
      "message": "...one or two plain sentences...",
      "steps": ["...", "..."],           # what to do, in order
      "action": {"label": "...", "do": "open_property_picker"} | {"label": "...", "href": "https://..."},
      "helpUrl": "https://support.google.com/...",
      "technical": "raw provider text, for support",
    }

Error responses keep their existing ``detail`` (string or {code, message}) so older screens keep
working, and add ``problem``. Successful responses may carry ``warnings: [problem, ...]``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

GSC_USERS_HELP = "https://support.google.com/webmasters/answer/7687615"
GA4_HOME = "https://analytics.google.com/analytics/web/"

AREA_LABELS = {
    "keywords": "Keywords",
    "backlinks": "Backlinks",
    "ai_visibility": "AI visibility",
    "site_audit": "Site audit",
    "site_scan": "Site scan",
    "search_console": "Search Console",
    "analytics": "Analytics",
    "google": "Google connection",
}

# Shared templates. ``{area}`` is replaced with the area label.
_CATALOG: dict[str, dict[str, Any]] = {
    # --- setup / plan ------------------------------------------------------------------
    "site_required": {
        "title": "Add your website first",
        "steps": ["Open Setup and enter the website address (for example example.com).", "Come back here and run {area} again."],
        "action": {"label": "Open setup", "do": "open_setup"},
    },
    "location_required": {
        "title": "Choose a target market",
        "steps": ["Pick the country (and city, if local) you want to rank in.", "Run {area} again."],
        "action": {"label": "Choose market", "do": "open_market"},
    },
    "quota_exceeded": {
        "title": "Plan limit reached",
        "steps": ["This plan's {area} allowance for the period is used.", "Wait for the reset, or upgrade for a higher limit."],
        "action": {"label": "See plans", "do": "open_plans"},
    },
    "daily_limit": {
        "title": "Today's research limit is used",
        "steps": ["The daily lookup limit for this plan resets tomorrow (UTC).", "Saved results stay visible until then.", "Upgrade for a higher daily limit."],
        "action": {"label": "See plans", "do": "open_plans"},
    },
    # --- research provider (DataForSEO, shown as "Searchify SEO") -------------------------
    "provider_not_configured": {
        "title": "Research service is not connected",
        "steps": ["This is a setup issue on Searchify's side, not on your website.", "Contact support; saved results stay visible meanwhile."],
        "action": {"label": "Contact support", "do": "contact_support"},
    },
    "provider_auth": {
        "title": "Research service sign-in failed",
        "steps": ["Searchify's research account could not sign in. Nothing is wrong with your site.", "Contact support so an admin can check the research connection."],
        "action": {"label": "Contact support", "do": "contact_support"},
    },
    "provider_api_disabled": {
        "title": "This research feature is not switched on",
        "steps": ["The provider subscription for {area} is not active on Searchify's account.", "Contact support to enable it."],
        "action": {"label": "Contact support", "do": "contact_support"},
    },
    "provider_balance": {
        "title": "Research credit is empty",
        "steps": ["Searchify's research credit ran out, so live {area} can't run.", "Saved results stay visible. Contact support or try again after a top-up."],
        "action": {"label": "Contact support", "do": "contact_support"},
    },
    "provider_cost_limit": {
        "title": "Research spending cap reached",
        "steps": ["The research provider's daily spending cap was hit.", "Try again tomorrow, or ask support to raise the cap."],
        "action": {"label": "Contact support", "do": "contact_support"},
    },
    "provider_paused": {
        "title": "Research access is paused",
        "steps": ["The research provider paused Searchify's access for a security review.", "Contact support; this is not caused by your site."],
        "action": {"label": "Contact support", "do": "contact_support"},
    },
    "provider_busy": {
        "title": "Research service is busy",
        "steps": ["Too many requests ran at once. Wait about a minute.", "Then run {area} again."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "provider_timeout": {
        "title": "The request took too long",
        "steps": ["The provider or the target website did not answer in time.", "Try again in a few minutes. If your site is slow, check it loads in a browser."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "provider_unavailable": {
        "title": "Research service is temporarily unavailable",
        "steps": ["The provider reported a temporary outage or update.", "Try again in a few minutes. Saved results stay visible."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "provider_unreachable": {
        "title": "Could not reach the research service",
        "steps": ["A network problem stopped the request.", "Try again in a minute."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "provider_no_results": {
        "title": "No data for this request",
        "steps": ["The provider has no data for this site or market yet. New or very small sites often have none.", "Try broader keywords or another market, or check back in a few weeks."],
        "action": None,
    },
    "target_invalid": {
        "title": "The website address could not be read",
        "steps": ["Check the address opens in a browser (https://…).", "Fix it in Setup if it changed, then run {area} again."],
        "action": {"label": "Open setup", "do": "open_setup"},
    },
    "provider_error": {
        "title": "{area} could not finish",
        "steps": ["The research provider returned an error.", "Try again. If it keeps failing, contact support with the details below."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "research_error": {
        "title": "{area} could not finish",
        "steps": ["Try again. If it keeps failing, contact support with the details below."],
        "action": {"label": "Try again", "do": "retry"},
    },
    # --- Google ---------------------------------------------------------------------------
    "google_not_connected": {
        "title": "Google is not connected",
        "steps": ["Connect the Google account that has access to this site's Search Console and Analytics."],
        "action": {"label": "Connect Google", "do": "reconnect_google"},
    },
    "google_reauth_required": {
        "title": "Google sign-in expired",
        "steps": ["Google access was revoked or expired.", "Connect Google again with the same account. Your property choices are kept."],
        "action": {"label": "Reconnect Google", "do": "reconnect_google"},
    },
    "google_network": {
        "title": "Could not reach Google",
        "steps": ["A network problem stopped the sync.", "Try again in a minute."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "gsc_not_selected": {
        "title": "No Search Console property chosen",
        "steps": ["Pick the Search Console property for this website.", "Save and sync."],
        "action": {"label": "Choose property", "do": "open_property_picker"},
    },
    "ga4_not_selected": {
        "title": "No Analytics property chosen",
        "steps": ["Pick the GA4 property for this website to add traffic and landing-page data.", "Save and sync."],
        "action": {"label": "Choose property", "do": "open_property_picker"},
    },
    "ga4_no_access": {
        "title": "Google can't read this Analytics property",
        "steps": [
            "In Google Analytics, open Admin > Property access management.",
            "Add {email} with at least Viewer access to this GA4 property.",
            "Back in Searchify, choose the property again and Save and sync.",
        ],
        "action": {"label": "Open Google Analytics", "href": GA4_HOME},
    },
    "gsc_no_data": {
        "title": "Search Console returned no data",
        "steps": [
            "The property has no clicks or impressions in the last 28 days.",
            "New or recently verified properties can take a few days to fill in.",
            "If the site has traffic, check you picked the property that matches the live address (www vs non-www, http vs https).",
        ],
        "action": {"label": "Check property", "do": "open_property_picker"},
    },
    "ga4_no_data": {
        "title": "Analytics returned no data",
        "steps": ["The GA4 property recorded no sessions in the last 28 days.", "Check the GA4 tag is installed on the live site, or pick the property that is."],
        "action": {"label": "Check property", "do": "open_property_picker"},
    },
    # --- site audit / scan ----------------------------------------------------------------
    "audit_few_pages": {
        "title": "Only {pages} page(s) could be crawled",
        "steps": [
            "The crawler could only reach {pages} page(s), so the audit covers very little of the site.",
            "Common causes: robots.txt blocks crawlers, a firewall/CDN (e.g. Cloudflare) blocks bots, or links are added only by JavaScript.",
            "Allow crawlers on the site, then crawl again.",
        ],
        "action": {"label": "Crawl again", "do": "retry"},
    },
    "audit_page_limit": {
        "title": "Crawl stopped at the plan's page limit",
        "steps": ["The audit covers the first {pages} pages. Larger sites need a higher plan for a full crawl."],
        "action": {"label": "See plans", "do": "open_plans"},
    },
    "audit_stalled": {
        "title": "The crawl is taking longer than expected",
        "steps": ["Big or slow sites can take a while. Check back in a few minutes.", "If it is still stuck after an hour, start a new crawl."],
        "action": {"label": "Start new crawl", "do": "retry"},
    },
    "audit_no_js": {
        "title": "JavaScript is not rendered in this audit",
        "steps": ["Content added only by JavaScript is not seen by this crawl.", "Core Web Vitals come from the PageSpeed check, not from this crawl."],
        "action": None,
    },
    "scan_site_unreachable": {
        "title": "The website could not be opened",
        "steps": ["Check the address opens in a browser.", "If it does, the site may block automated visitors (firewall/CDN). Allow Searchify, then scan again."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "scan_no_pages": {
        "title": "No readable pages were found",
        "steps": ["The pages did not return HTML (they may need JavaScript or a login).", "Check the home page opens without signing in, then scan again."],
        "action": {"label": "Try again", "do": "retry"},
    },
    "scan_pages_failed": {
        "title": "{count} page(s) could not be read",
        "steps": [
            "Those pages returned an error or no HTML, so they are missing from the scan.",
            "Open them in a browser. Fix broken pages or redirect them, then scan again.",
        ],
        "action": {"label": "Scan again", "do": "retry"},
    },
    # --- AI visibility ----------------------------------------------------------------------
    "visibility_partial": {
        "title": "{failed} of {total} AI checks did not finish",
        "steps": ["Those prompts show their last saved answer (marked stale) or no answer.", "Run the check again later; providers sometimes time out."],
        "action": {"label": "Run again", "do": "retry"},
    },
    "visibility_soon": {
        "title": "Some AI engines are not available yet",
        "steps": ["Prompts set to {engines} are skipped for now.", "Switch those prompts to an available engine to include them."],
        "action": None,
    },
    "profile_required": {
        "title": "Add your business details first",
        "steps": ["Open Profile and fill in the business name and the services you offer.", "Run {area} again."],
        "action": {"label": "Open profile", "do": "open_profile"},
    },
    "wordpress_required": {
        "title": "Connect your website first",
        "steps": ["Connect WordPress (or choose the page URL) so the fix knows where to go.", "Then send it to the work queue again."],
        "action": {"label": "Open connections", "do": "open_connections"},
    },
    "visibility_not_estimated": {
        "title": "{count} of {total} AI checks could not be estimated",
        "steps": [
            "The AI estimate did not return a usable answer for those checks.",
            "Make sure the profile names the business, services and city, then run the report again.",
        ],
        "action": {"label": "Run again", "do": "retry"},
    },
    "keywords_empty": {
        "title": "No keyword data for this market",
        "steps": ["The provider has no search volume for these terms in this market.", "Try broader terms or a larger market (country instead of city)."],
        "action": {"label": "Choose market", "do": "open_market"},
    },
    "backlinks_empty": {
        "title": "No backlinks found yet",
        "steps": ["No other sites linking to this domain were found. This is normal for new sites.", "See the link ideas below to earn the first links."],
        "action": None,
    },
}

_DEFAULT_SEVERITY = {
    "provider_no_results": "warning",
    "gsc_no_data": "warning",
    "ga4_no_data": "warning",
    "ga4_not_selected": "warning",
    "audit_few_pages": "warning",
    "audit_page_limit": "info",
    "audit_stalled": "warning",
    "audit_no_js": "info",
    "visibility_partial": "warning",
    "visibility_soon": "info",
    "scan_pages_failed": "warning",
    "visibility_not_estimated": "warning",
    "keywords_empty": "warning",
    "backlinks_empty": "info",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fmt(text: str, values: dict) -> str:
    return text.format_map(_SafeDict(values))


def make(
    code: str,
    area: str,
    *,
    message: str = "",
    severity: str | None = None,
    technical: str = "",
    steps: list[str] | None = None,
    action: dict | None | bool = False,
    title: str | None = None,
    **values: Any,
) -> dict:
    """Build a problem dict from the catalog. Extra keyword values fill {placeholders}."""
    template = _CATALOG.get(code) or _CATALOG["research_error"]
    values = {"area": AREA_LABELS.get(area, area), "email": "this Google account", **{k: v for k, v in values.items() if v not in (None, "")}}
    chosen_action = template.get("action") if action is False else action
    problem = {
        "code": code,
        "area": area,
        "severity": severity or _DEFAULT_SEVERITY.get(code, "error"),
        "title": _fmt(title or template.get("title") or "Something went wrong", values),
        "message": message or "",
        "steps": [_fmt(step, values) for step in (steps if steps is not None else template.get("steps") or [])],
        "action": ({k: _fmt(str(v), values) for k, v in chosen_action.items()} if chosen_action else None),
        "helpUrl": template.get("helpUrl") or "",
        "technical": (technical or "")[:400],
    }
    if not problem["message"]:
        problem["message"] = problem["steps"][0] if problem["steps"] else problem["title"]
    return problem


# --- research provider codes --------------------------------------------------------------

def research_code_for_provider(status_code: int) -> str:
    """Map a DataForSEO status_code to our problem code."""
    if status_code in (40200, 40210):
        return "provider_balance"
    if status_code in (40100, 40104):
        return "provider_auth"
    if status_code == 40204:
        return "provider_api_disabled"
    if status_code == 40203:
        return "provider_cost_limit"
    if status_code in (40201, 40207):
        return "provider_paused"
    if status_code in (40202, 40205, 40206, 40209):
        return "provider_busy"
    if status_code in (50401, 50402):
        return "provider_timeout"
    if status_code in (50000, 50001, 50301, 50303, 50304, 40101, 40103):
        return "provider_unavailable"
    if status_code in (40102, 40404):
        return "provider_no_results"
    if status_code in (40402, 40408, 40405):
        return "target_invalid"
    if status_code == 40505:
        return "location_required"
    return "provider_error"


# --- Google -------------------------------------------------------------------------------

def gsc_users_url(prop: str) -> str:
    return f"https://search.google.com/search-console/users?resource_id={quote(prop, safe='')}" if prop else GSC_USERS_HELP


def gsc_no_access(prop: str, email: str = "", alternatives: list[dict] | None = None, technical: str = "") -> dict:
    """The selected GSC property is not readable by the connected account.

    ``alternatives`` are properties for the SAME site this account CAN read; when present the
    fastest fix is to pick one of them, not to ask the client for access.
    """
    who = (email or "").strip() or "the connected Google account"
    kind = "Domain property" if prop.lower().startswith("sc-domain:") else "URL-prefix property"
    usable = [a for a in (alternatives or []) if a.get("siteUrl") and a.get("siteUrl") != prop]
    if usable:
        best = usable[0]
        level = {"siteOwner": "Owner", "siteFullUser": "Full", "siteRestrictedUser": "Restricted"}.get(best.get("permissionLevel") or "", best.get("permissionLevel") or "")
        return make(
            "gsc_property_no_access",
            "search_console",
            title="Saved Search Console property can't be read",
            message=(
                f"Searchify is using {prop} ({kind}), which {who} can't read. "
                f"{who} already has {level or 'access'} on {best['siteUrl']} for the same site."
            ),
            steps=[
                f"Choose {best['siteUrl']} in the Search Console property list.",
                "Click Save and sync.",
                f"Only if you need the {kind} itself: ask the site owner to add {who} as Full on {prop}.",
            ],
            action={"label": f"Use {best['siteUrl']}", "do": "open_property_picker", "value": best["siteUrl"]},
            technical=technical,
            alternatives=[a["siteUrl"] for a in usable],
        ) | {"alternatives": [{"siteUrl": a["siteUrl"], "permissionLevel": a.get("permissionLevel") or ""} for a in usable]}
    return make(
        "gsc_property_no_access",
        "search_console",
        title="Google can't read this Search Console property",
        message=f"{who} has no access to {prop} ({kind}).",
        steps=[
            "Ask the site owner to open Search Console > Settings > Users and permissions.",
            f"Add {who} as Full on {prop} exactly (a Domain property and an https:// property are separate).",
            f"Sign in to Searchify with {who}, choose the property again, then Save and sync.",
        ],
        action={"label": "Open Users and permissions", "href": gsc_users_url(prop)},
        technical=technical,
    ) | {"helpUrl": GSC_USERS_HELP}


def same_site_properties(sites: list[dict], prop: str) -> list[dict]:
    """Readable GSC properties covering the same host as ``prop`` (best permission first)."""
    from app.google_match import UNUSABLE, bare_host

    host = bare_host(prop)
    rank = {"siteOwner": 0, "siteFullUser": 1, "siteRestrictedUser": 2}
    out = [
        s
        for s in sites or []
        if s.get("siteUrl")
        and s.get("permissionLevel") not in UNUSABLE
        and bare_host(s["siteUrl"]) == host
    ]
    return sorted(out, key=lambda s: (rank.get(s.get("permissionLevel") or "", 9), not str(s["siteUrl"]).startswith("https://")))


# --- HTTP plumbing ------------------------------------------------------------------------

class ProblemError(HTTPException):
    """HTTPException that also returns a structured ``problem`` next to ``detail``."""

    def __init__(self, status_code: int, problem: dict, detail: Any = None):
        super().__init__(status_code=status_code, detail=detail if detail is not None else problem.get("message"))
        self.problem = problem


async def problem_exception_handler(_request: Request, exc: ProblemError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "problem": exc.problem}, headers=getattr(exc, "headers", None))


# --- warnings on successful results -------------------------------------------------------

def research_warnings(area: str, result: dict) -> list[dict]:
    """Non-fatal caveats for a research result (shown above the report, each with help)."""
    out: list[dict] = []
    if not isinstance(result, dict) or result.get("state") == "none":
        return out  # nothing fetched yet is not a problem
    if area == "keywords":
        rows = (result.get("ranked") or []) + (result.get("tracked") or []) + (result.get("ideas") or [])
        if not rows and not result.get("storedOnly"):
            out.append(make("keywords_empty", area))
    elif area == "backlinks":
        summary = result.get("summary") or {}
        if not (result.get("links") or []) and not (summary.get("referringDomains") or 0):
            out.append(make("backlinks_empty", area))
    elif area == "ai_visibility":
        results = result.get("results") or []
        failed = [r for r in results if r.get("status") == "error" and r.get("code") != "engine_unavailable"]
        if failed:
            stale = sum(1 for r in failed if r.get("stale"))
            out.append(
                make(
                    "visibility_partial",
                    area,
                    failed=len(failed),
                    total=len(results),
                    technical="; ".join(sorted({str(r.get("error") or "")[:120] for r in failed if r.get("error")}))[:400],
                    message=(f"{stale} show their last saved answer." if stale else ""),
                )
            )
        soon = sorted({str(r.get("engine")) for r in results if r.get("code") == "engine_unavailable" and r.get("engine")})
        if soon:
            out.append(make("visibility_soon", area, engines=", ".join(soon)))
    elif area == "site_audit":
        report = result.get("report") or {}
        if result.get("state") == "crawling" and result.get("startedAt"):
            from datetime import datetime, timedelta

            try:
                started = datetime.fromisoformat(result["startedAt"])
            except ValueError:
                started = None
            if started and datetime.utcnow() - started > timedelta(minutes=45) and not result.get("crawled"):
                out.append(make("audit_stalled", area))
        if result.get("state") == "ready" and report:
            crawled = int(report.get("pagesCrawled") or 0)
            limit = int(report.get("maxPages") or result.get("maxPages") or 0)
            if crawled and crawled <= 2:
                out.append(make("audit_few_pages", area, pages=crawled))
            elif limit and crawled >= limit:
                out.append(make("audit_page_limit", area, pages=limit))
            out.append(make("audit_no_js", area))
    return out


def ai_report_warnings(report: dict | None) -> list[dict]:
    """Warnings for the likelihood (operator) AI visibility report."""
    checks = (report or {}).get("checks") or []
    missing = [c for c in checks if c.get("status") == "not_checked"]
    if not checks or not missing:
        return []
    return [
        make(
            "visibility_not_estimated",
            "ai_visibility",
            count=len(missing),
            total=len(checks),
            technical=str(missing[0].get("error") or "")[:300],
        )
    ]


def scan_error_problem(error: str, failed: list | None = None, area: str = "site_scan") -> dict:
    """Problem for a site scan that read no pages."""
    text = str(error or "")
    code = "target_invalid" if "not a public website" in text else "scan_no_pages" if "No readable pages" in text else "scan_site_unreachable"
    technical = "; ".join(f"{f.get('url')}: {f.get('error')}" for f in (failed or [])[:5])
    return make(code, area, message=text, technical=technical, action=None if code == "target_invalid" else False)


def scan_warnings(scan: dict | None) -> list[dict]:
    failed = (scan or {}).get("failedPages") or (scan or {}).get("failed") or []
    if not failed:
        return []
    technical = "\n".join(f"{f.get('url')} → {f.get('error')}" for f in failed[:10])
    return [make("scan_pages_failed", "site_scan", count=len(failed), technical=technical)]
