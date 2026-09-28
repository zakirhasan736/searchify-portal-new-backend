"""Google OAuth + GSC/GA4 helpers for Searchify."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt

from app import config

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
    ADS_SCOPE,
]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GSC_SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"
GA4_ACCOUNT_SUMMARIES = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
GA4_RUN_REPORT = "https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"


def google_configured() -> bool:
    return bool(config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET)


def make_oauth_state(user_id: int, services: str = "gsc,ga4") -> str:
    payload = {
        "uid": user_id,
        "services": services,
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def parse_oauth_state(state: str) -> dict:
    return jwt.decode(state, config.JWT_SECRET, algorithms=["HS256"])


def scopes_for(services: str = "") -> list[str]:
    # One Google sign-in covers Search Console, GA4, and Ads.
    return list(SCOPES)


def ads_scope_granted(scopes: list | None) -> bool:
    return any("adwords" in str(item) for item in (scopes or []))


def auth_url(state: str, services: str = "", force_consent: bool = True) -> str:
    params = {
        "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": config.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes_for(services)),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
    }
    if force_consent:
        params["prompt"] = "consent"
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": config.GOOGLE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"token exchange failed: {response.text[:400]}")
    return response.json()


def refresh_access_token(refresh_token: str) -> dict:
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"token refresh failed: {response.text[:400]}")
    return response.json()


def fetch_userinfo(access_token: str) -> dict:
    with httpx.Client(timeout=20.0) as client:
        response = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    if response.status_code >= 400:
        return {}
    return response.json()


def list_gsc_sites(access_token: str) -> list[dict]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(GSC_SITES_URL, headers={"Authorization": f"Bearer {access_token}"})
    if response.status_code >= 400:
        raise RuntimeError(f"GSC sites failed: {response.text[:300]}")
    entries = response.json().get("siteEntry") or []
    return [
        {
            "siteUrl": row.get("siteUrl") or "",
            "permissionLevel": row.get("permissionLevel") or "",
        }
        for row in entries
        if row.get("siteUrl")
    ]


def list_ga4_properties(access_token: str) -> list[dict]:
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            GA4_ACCOUNT_SUMMARIES,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"GA4 properties failed: {response.text[:300]}")
    out: list[dict] = []
    for account in response.json().get("accountSummaries") or []:
        account_name = account.get("displayName") or account.get("account") or ""
        for prop in account.get("propertySummaries") or []:
            raw = prop.get("property") or ""
            # property is like "properties/123456"
            prop_id = raw.split("/")[-1] if raw else ""
            if not prop_id:
                continue
            out.append(
                {
                    "propertyId": prop_id,
                    "displayName": prop.get("displayName") or prop_id,
                    "accountName": account_name,
                }
            )
    return out


def fetch_gsc_dimension(
    access_token: str,
    site_url: str,
    dimension: str,
    days: int = 28,
    row_limit: int = 25,
) -> list[list]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    body = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "dimensions": [dimension],
        "rowLimit": row_limit,
    }
    from urllib.parse import quote

    url = f"https://www.googleapis.com/webmasters/v3/sites/{quote(site_url, safe='')}/searchAnalytics/query"
    with httpx.Client(timeout=45.0) as client:
        response = client.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"GSC {dimension} failed: {response.text[:300]}")
    rows = []
    for row in response.json().get("rows") or []:
        keys = row.get("keys") or [""]
        rows.append(
            [
                keys[0],
                str(int(row.get("clicks") or 0)),
                str(int(row.get("impressions") or 0)),
                f"{(row.get('ctr') or 0) * 100:.1f}%",
                f"{row.get('position') or 0:.1f}",
            ]
        )
    return rows


def fetch_gsc_top_queries(access_token: str, site_url: str, days: int = 28, row_limit: int = 25) -> list[list]:
    return fetch_gsc_dimension(access_token, site_url, "query", days, row_limit)


def fetch_gsc_top_pages(access_token: str, site_url: str, days: int = 28, row_limit: int = 25) -> list[list]:
    return fetch_gsc_dimension(access_token, site_url, "page", days, row_limit)


def fetch_gsc_by_country(access_token: str, site_url: str, days: int = 28, row_limit: int = 15) -> list[list]:
    return fetch_gsc_dimension(access_token, site_url, "country", days, row_limit)


def fetch_gsc_by_device(access_token: str, site_url: str, days: int = 28, row_limit: int = 10) -> list[list]:
    return fetch_gsc_dimension(access_token, site_url, "device", days, row_limit)


def fetch_gsc_daily(access_token: str, site_url: str, days: int = 28) -> list[list]:
    """Daily Search Console totals: [date YYYY-MM-DD, clicks, impressions, ctr, position]."""
    rows = fetch_gsc_dimension(access_token, site_url, "date", days=days, row_limit=max(days + 2, 32))
    rows.sort(key=lambda r: r[0] or "")
    return rows


def gsc_weekly_clicks(daily: list[list], weeks: int = 4) -> list[list]:
    """Bucket daily GSC rows into weekly [label, clicks] for Performance chart."""
    if not daily:
        return []
    ordered = sorted(daily, key=lambda r: r[0] or "")
    buckets: list[list] = []
    i = 0
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    while i < len(ordered):
        chunk = ordered[i : i + 7]
        if not chunk:
            break
        clicks = sum(int(float(r[1] or 0)) for r in chunk)
        start = chunk[0][0] or ""
        try:
            _y, m, d = start.split("-")
            label = f"{months[int(m) - 1]} {int(d)}"
        except Exception:  # noqa: BLE001
            label = start[5:] if len(start) >= 7 else start
        buckets.append([label, clicks])
        i += 7
    return buckets[-weeks:] if len(buckets) > weeks else buckets


def fetch_ga4_report(
    access_token: str,
    property_id: str,
    *,
    dimensions: list[str],
    metrics: list[str],
    days: int = 28,
    limit: int = 25,
) -> list[list]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    body = {
        "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
        "metrics": [{"name": m} for m in metrics],
        "dimensions": [{"name": d} for d in dimensions],
        "limit": limit,
    }
    url = GA4_RUN_REPORT.format(property_id=property_id)
    with httpx.Client(timeout=45.0) as client:
        response = client.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"GA4 report failed: {response.text[:300]}")
    out = []
    for row in response.json().get("rows") or []:
        dims = [d.get("value") or "" for d in (row.get("dimensionValues") or [])]
        mets = []
        for m in row.get("metricValues") or []:
            raw = m.get("value") or "0"
            try:
                num = float(raw)
                mets.append(str(int(num)) if num == int(num) else f"{num:.2f}")
            except ValueError:
                mets.append(raw)
        out.append(dims + mets)
    return out


def fetch_ga4_overview(access_token: str, property_id: str, days: int = 28) -> dict:
    channel_rows = fetch_ga4_report(
        access_token,
        property_id,
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "totalUsers", "screenPageViews"],
        days=days,
        limit=20,
    )
    sessions = users = views = 0
    for row in channel_rows:
        sessions += int(float(row[1] or 0))
        users += int(float(row[2] or 0))
        views += int(float(row[3] or 0))
    landing = fetch_ga4_report(
        access_token,
        property_id,
        dimensions=["landingPagePlusQueryString"],
        metrics=["sessions", "totalUsers", "bounceRate"],
        days=days,
        limit=20,
    )
    daily = fetch_ga4_report(
        access_token,
        property_id,
        dimensions=["date"],
        metrics=["sessions", "totalUsers"],
        days=days,
        limit=days + 2,
    )
    # GA4 date is YYYYMMDD — normalize for charts
    daily_norm = []
    for row in daily:
        raw = row[0] or ""
        if len(raw) == 8 and raw.isdigit():
            label = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        else:
            label = raw
        daily_norm.append([label, row[1], row[2] if len(row) > 2 else "0"])
    daily_norm.sort(key=lambda r: r[0])

    referral = fetch_ga4_report(
        access_token,
        property_id,
        dimensions=["sessionSource", "sessionMedium"],
        metrics=["sessions", "totalUsers", "screenPageViews"],
        days=days,
        limit=30,
    )
    referral_rows = []
    ai_rows = []
    ai_hosts = (
        "chatgpt",
        "openai",
        "perplexity",
        "gemini",
        "copilot",
        "bing.com/chat",
        "you.com",
        "claude",
        "anthropic",
        "neeva",
        "phind",
    )
    for row in referral:
        source = (row[0] or "").lower()
        medium = (row[1] or "").lower()
        label = f"{row[0]} / {row[1]}"
        if medium in {"referral", "organic"} or "referral" in medium:
            referral_rows.append([label, row[2], row[3], row[4] if len(row) > 4 else "0"])
        if any(h in source for h in ai_hosts) or medium in {"ai", "chatgpt"}:
            ai_rows.append([label, row[2], row[3], row[4] if len(row) > 4 else "0"])
    if not referral_rows:
        referral_rows = [[f"{r[0]} / {r[1]}", r[2], r[3], r[4] if len(r) > 4 else "0"] for r in referral[:15]]

    return {
        "sessions": sessions,
        "users": users,
        "views": views,
        "channels": channel_rows,
        "landingPages": landing,
        "daily": daily_norm,
        "referral": referral_rows,
        "aiTraffic": ai_rows,
    }


def run_pagespeed(url: str, strategy: str = "mobile") -> dict:
    if not config.GOOGLE_PAGESPEED_API_KEY:
        raise RuntimeError("GOOGLE_PAGESPEED_API_KEY is not set")
    params = {
        "url": url,
        "strategy": strategy,
        "category": ["performance", "accessibility", "best-practices", "seo"],
        "key": config.GOOGLE_PAGESPEED_API_KEY,
    }
    with httpx.Client(timeout=120.0) as client:
        response = client.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"PageSpeed failed: {response.text[:300]}")
    data = response.json()
    lighthouse = data.get("lighthouseResult") or {}
    cats = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}
    config_block = lighthouse.get("configSettings") or {}

    def score(key: str) -> str:
        val = (cats.get(key) or {}).get("score")
        return str(int(round((val or 0) * 100))) if val is not None else "—"

    def score_num(key: str) -> int | None:
        val = (cats.get(key) or {}).get("score")
        return int(round((val or 0) * 100)) if val is not None else None

    def metric(key: str) -> str:
        item = audits.get(key) or {}
        return str(item.get("displayValue") or item.get("numericValue") or "—")

    def audit_rating(key: str) -> str:
        item = audits.get(key) or {}
        s = item.get("score")
        if s is None:
            return "—"
        if s >= 0.9:
            return "good"
        if s >= 0.5:
            return "needs improvement"
        return "poor"

    def audit_rows_for_category(cat_key: str, limit: int = 40) -> list[list]:
        refs = (cats.get(cat_key) or {}).get("auditRefs") or []
        rows = []
        for ref in refs:
            aid = ref.get("id") or ""
            item = audits.get(aid) or {}
            mode = item.get("scoreDisplayMode") or ""
            if mode in {"informative", "manual", "notApplicable"} and item.get("score") is None:
                continue
            title = item.get("title") or aid
            display = item.get("displayValue") or ""
            s = item.get("score")
            if s is None:
                status = mode or "info"
            elif s >= 0.9:
                status = "Pass"
            elif s >= 0.5:
                status = "Average"
            else:
                status = "Fail"
            weight = ref.get("weight")
            rows.append(
                [
                    title,
                    status,
                    display or ("—" if s is None else f"{int(round(s * 100))}"),
                    str(weight if weight is not None else "—"),
                    (item.get("description") or "")[:160],
                ]
            )
        # Failures first
        order = {"Fail": 0, "Average": 1, "Pass": 2}
        rows.sort(key=lambda r: order.get(r[1], 3))
        return rows[:limit]

    opportunities: list[list] = []
    diagnostics: list[list] = []
    for aid, item in audits.items():
        details = item.get("details") or {}
        dtype = details.get("type") or ""
        title = item.get("title") or aid
        display = item.get("displayValue") or ""
        desc = (item.get("description") or "")[:180]
        s = item.get("score")
        if dtype == "opportunity":
            savings = ""
            overall = details.get("overallSavingsMs")
            if overall is not None:
                savings = f"{int(overall)} ms"
            elif details.get("overallSavingsBytes") is not None:
                savings = f"{int(details['overallSavingsBytes'])} bytes"
            opportunities.append([title, display or savings or "—", savings or display or "—", desc])
        elif dtype == "table" and s is not None and s < 1:
            diagnostics.append([title, display or "—", "Issue" if s < 0.9 else "Info", desc])
        elif item.get("scoreDisplayMode") == "informative" and aid in {
            "dom-size",
            "font-display",
            "third-party-summary",
            "mainthread-work-breakdown",
            "bootup-time",
            "network-requests",
            "network-rtt",
            "network-server-latency",
            "redirects",
            "uses-http2",
            "total-byte-weight",
        }:
            diagnostics.append([title, display or "—", "Diagnostic", desc])

    opportunities.sort(key=lambda r: 0 if "ms" in r[2] else 1)
    opportunities = opportunities[:25]
    diagnostics = diagnostics[:30]

    # CrUX field data when present
    def crux_metrics(block: dict | None) -> list[list]:
        if not block:
            return []
        metrics = block.get("metrics") or {}
        out = []
        labels = {
            "LARGEST_CONTENTFUL_PAINT_MS": "LCP (field)",
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": "CLS (field)",
            "INTERACTION_TO_NEXT_PAINT": "INP (field)",
            "FIRST_CONTENTFUL_PAINT_MS": "FCP (field)",
            "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "TTFB (field)",
        }
        for key, label in labels.items():
            m = metrics.get(key) or {}
            p75 = m.get("percentile")
            cat = (m.get("category") or "—").lower()
            out.append([label, str(p75) if p75 is not None else "—", cat])
        return out

    loading = crux_metrics(data.get("loadingExperience"))
    origin_loading = crux_metrics(data.get("originLoadingExperience"))

    screenshot = ""
    shot = audits.get("final-screenshot") or {}
    details = shot.get("details") or {}
    if details.get("data"):
        screenshot = str(details.get("data"))[:200]  # data URL can be huge — store flag only in payload later
        screenshot = "available" if details.get("data") else ""

    final_url = lighthouse.get("finalUrl") or lighthouse.get("requestedUrl") or url
    fetch_time = lighthouse.get("fetchTime") or ""
    user_agent = (lighthouse.get("userAgent") or "")[:120]

    return {
        "url": url,
        "finalUrl": final_url,
        "strategy": strategy,
        "fetchTime": fetch_time,
        "formFactor": config_block.get("formFactor") or strategy,
        "userAgent": user_agent,
        "screenshot": screenshot,
        "scores": {
            "performance": score("performance"),
            "accessibility": score("accessibility"),
            "bestPractices": score("best-practices"),
            "seo": score("seo"),
        },
        "scoreNums": {
            "performance": score_num("performance"),
            "accessibility": score_num("accessibility"),
            "bestPractices": score_num("best-practices"),
            "seo": score_num("seo"),
        },
        "vitals": {
            "LCP": metric("largest-contentful-paint"),
            "CLS": metric("cumulative-layout-shift"),
            "INP": metric("interaction-to-next-paint"),
            "TBT": metric("total-blocking-time"),
            "FCP": metric("first-contentful-paint"),
            "SI": metric("speed-index"),
            "TTI": metric("interactive"),
            "TTFB": metric("server-response-time"),
        },
        "vitalRatings": {
            "LCP": audit_rating("largest-contentful-paint"),
            "CLS": audit_rating("cumulative-layout-shift"),
            "INP": audit_rating("interaction-to-next-paint"),
            "FCP": audit_rating("first-contentful-paint"),
            "TBT": audit_rating("total-blocking-time"),
        },
        "opportunities": opportunities,
        "diagnostics": diagnostics,
        "seoAudits": audit_rows_for_category("seo"),
        "a11yAudits": audit_rows_for_category("accessibility"),
        "bestPracticeAudits": audit_rows_for_category("best-practices"),
        "perfAudits": audit_rows_for_category("performance", limit=30),
        "fieldMetrics": loading,
        "originFieldMetrics": origin_loading,
    }


def build_site_audit_payload(
    *,
    url: str,
    mobile: dict,
    desktop: dict | None = None,
    gsc_pages: list[list] | None = None,
    gsc_queries: list[list] | None = None,
) -> dict:
    """Rich feature payload for Site Audit tool."""
    desktop = desktop or {}
    m_scores = mobile.get("scores") or {}
    d_scores = desktop.get("scores") or {}
    m_vitals = mobile.get("vitals") or {}
    d_vitals = desktop.get("vitals") or {}

    overview_rows = [
        ["URL tested", mobile.get("finalUrl") or url, mobile.get("strategy") or "mobile", mobile.get("fetchTime") or "—"],
        ["Performance", m_scores.get("performance", "—"), d_scores.get("performance", "—"), "Lighthouse"],
        ["SEO", m_scores.get("seo", "—"), d_scores.get("seo", "—"), "Lighthouse"],
        ["Accessibility", m_scores.get("accessibility", "—"), d_scores.get("accessibility", "—"), "Lighthouse"],
        ["Best practices", m_scores.get("bestPractices", "—"), d_scores.get("bestPractices", "—"), "Lighthouse"],
        ["LCP", m_vitals.get("LCP", "—"), d_vitals.get("LCP", "—"), mobile.get("vitalRatings", {}).get("LCP", "—")],
        ["CLS", m_vitals.get("CLS", "—"), d_vitals.get("CLS", "—"), mobile.get("vitalRatings", {}).get("CLS", "—")],
        ["INP", m_vitals.get("INP", "—"), d_vitals.get("INP", "—"), mobile.get("vitalRatings", {}).get("INP", "—")],
        ["FCP", m_vitals.get("FCP", "—"), d_vitals.get("FCP", "—"), mobile.get("vitalRatings", {}).get("FCP", "—")],
        ["TBT", m_vitals.get("TBT", "—"), d_vitals.get("TBT", "—"), "Lab"],
        ["Speed Index", m_vitals.get("SI", "—"), d_vitals.get("SI", "—"), "Lab"],
        ["TTI", m_vitals.get("TTI", "—"), d_vitals.get("TTI", "—"), "Lab"],
        ["TTFB", m_vitals.get("TTFB", "—"), d_vitals.get("TTFB", "—"), "Lab"],
    ]

    gsc_findings: list[list] = []
    for page in (gsc_pages or [])[:15]:
        try:
            pos = float(page[4] or 0)
            ctr = float(str(page[3]).replace("%", "") or 0)
        except ValueError:
            pos, ctr = 0.0, 0.0
        issue = []
        if pos > 20:
            issue.append("Deep position")
        elif pos > 10:
            issue.append("Page 2+")
        if ctr < 2 and float(page[2] or 0) > 50:
            issue.append("Low CTR")
        if issue:
            gsc_findings.append([page[0][-60:], page[1], page[2], page[3], page[4], ", ".join(issue)])

    for q in (gsc_queries or [])[:10]:
        try:
            pos = float(q[4] or 0)
        except ValueError:
            pos = 0.0
        if pos > 10:
            gsc_findings.append([f"Query: {q[0][:50]}", q[1], q[2], q[3], q[4], "Opportunity to improve rank"])

    table_panels = [
        {
            "title": "Core Web Vitals · mobile",
            "columns": ["Vital", "Value", "Rating"],
            "rows": [
                ["LCP", m_vitals.get("LCP", "—"), mobile.get("vitalRatings", {}).get("LCP", "—")],
                ["CLS", m_vitals.get("CLS", "—"), mobile.get("vitalRatings", {}).get("CLS", "—")],
                ["INP", m_vitals.get("INP", "—"), mobile.get("vitalRatings", {}).get("INP", "—")],
                ["FCP", m_vitals.get("FCP", "—"), mobile.get("vitalRatings", {}).get("FCP", "—")],
                ["TBT", m_vitals.get("TBT", "—"), "lab"],
                ["Speed Index", m_vitals.get("SI", "—"), "lab"],
            ],
        },
        {
            "title": "Opportunities · mobile",
            "columns": ["Opportunity", "Estimate", "Savings", "Detail"],
            "rows": mobile.get("opportunities") or [],
        },
        {
            "title": "Diagnostics · mobile",
            "columns": ["Diagnostic", "Value", "Type", "Detail"],
            "rows": mobile.get("diagnostics") or [],
        },
        {
            "title": "SEO audits · mobile",
            "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
            "rows": mobile.get("seoAudits") or [],
        },
        {
            "title": "Accessibility audits · mobile",
            "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
            "rows": mobile.get("a11yAudits") or [],
        },
        {
            "title": "Best practices · mobile",
            "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
            "rows": mobile.get("bestPracticeAudits") or [],
        },
        {
            "title": "Performance audits · mobile",
            "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
            "rows": mobile.get("perfAudits") or [],
        },
    ]
    if desktop:
        table_panels.append(
            {
                "title": "Core Web Vitals · desktop",
                "columns": ["Vital", "Value", "Rating"],
                "rows": [
                    ["LCP", d_vitals.get("LCP", "—"), desktop.get("vitalRatings", {}).get("LCP", "—")],
                    ["CLS", d_vitals.get("CLS", "—"), desktop.get("vitalRatings", {}).get("CLS", "—")],
                    ["INP", d_vitals.get("INP", "—"), desktop.get("vitalRatings", {}).get("INP", "—")],
                    ["FCP", d_vitals.get("FCP", "—"), desktop.get("vitalRatings", {}).get("FCP", "—")],
                    ["TBT", d_vitals.get("TBT", "—"), "lab"],
                ],
            }
        )
        table_panels.append(
            {
                "title": "Opportunities · desktop",
                "columns": ["Opportunity", "Estimate", "Savings", "Detail"],
                "rows": desktop.get("opportunities") or [],
            }
        )
    if mobile.get("fieldMetrics"):
        table_panels.append(
            {
                "title": "Chrome UX Report · this URL",
                "columns": ["Metric", "p75", "Category"],
                "rows": mobile["fieldMetrics"],
            }
        )
    if mobile.get("originFieldMetrics"):
        table_panels.append(
            {
                "title": "Chrome UX Report · origin",
                "columns": ["Metric", "p75", "Category"],
                "rows": mobile["originFieldMetrics"],
            }
        )
    if gsc_findings:
        table_panels.append(
            {
                "title": "Search Console findings (related)",
                "columns": ["Page / Query", "Clicks", "Impressions", "CTR", "Position", "Issue"],
                "rows": gsc_findings,
            }
        )

    fail_seo = sum(1 for r in (mobile.get("seoAudits") or []) if r[1] == "Fail")
    fail_a11y = sum(1 for r in (mobile.get("a11yAudits") or []) if r[1] == "Fail")
    fail_bp = sum(1 for r in (mobile.get("bestPracticeAudits") or []) if r[1] == "Fail")

    return {
        "summary": (
            f"Full PageSpeed Insights audit for {mobile.get('finalUrl') or url} "
            f"(mobile perf {m_scores.get('performance', '—')}, SEO {m_scores.get('seo', '—')})."
        ),
        "columns": ["Item", "Mobile", "Desktop", "Note"],
        "rows": overview_rows,
        "kpis": [
            ["Mobile perf", m_scores.get("performance", "—")],
            ["Desktop perf", d_scores.get("performance", "—")],
            ["SEO", m_scores.get("seo", "—")],
            ["A11y fails", str(fail_a11y)],
            ["SEO fails", str(fail_seo)],
            ["Opportunities", str(len(mobile.get("opportunities") or []))],
        ],
        "panels": {
            "score": int(float(m_scores.get("performance") or 0) or 0),
            "scoreLabel": "Mobile performance",
            "audit": [
                ["SEO", m_scores.get("seo", "—")],
                ["Accessibility", m_scores.get("accessibility", "—")],
                ["Best practices", m_scores.get("bestPractices", "—")],
                ["Desktop perf", d_scores.get("performance", "—")],
                ["LCP", m_vitals.get("LCP", "—")],
                ["CLS", m_vitals.get("CLS", "—")],
                ["INP", m_vitals.get("INP", "—")],
            ],
            "kpis": [
                ["Perf", m_scores.get("performance", "—")],
                ["SEO", m_scores.get("seo", "—")],
                ["A11y", m_scores.get("accessibility", "—")],
                ["BP", m_scores.get("bestPractices", "—")],
            ],
        },
        "tablePanels": table_panels,
        "concept": "psi-report",
        "source": "google_pagespeed",
        "url": url,
        "finalUrl": mobile.get("finalUrl") or url,
        "seedVersion": "google-live-v1",
        "vitals": {"mobile": m_vitals, "desktop": d_vitals},
        "scores": {"mobile": m_scores, "desktop": d_scores},
        "counts": {
            "seoFails": fail_seo,
            "a11yFails": fail_a11y,
            "bestPracticeFails": fail_bp,
            "opportunities": len(mobile.get("opportunities") or []),
            "diagnostics": len(mobile.get("diagnostics") or []),
            "gscFindings": len(gsc_findings),
        },
    }


def places_text_search(query: str, max_results: int = 10) -> list[list]:
    if not config.GOOGLE_PLACES_API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not set")
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.websiteUri,places.googleMapsUri,places.id",
    }
    body = {"textQuery": query, "pageSize": max_results}
    with httpx.Client(timeout=30.0) as client:
        response = client.post("https://places.googleapis.com/v1/places:searchText", headers=headers, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"Places failed: {response.text[:300]}")
    rows = []
    for place in response.json().get("places") or []:
        name = (place.get("displayName") or {}).get("text") or ""
        rows.append(
            [
                name,
                place.get("formattedAddress") or "",
                str(place.get("rating") or "—"),
                str(place.get("userRatingCount") or "0"),
                place.get("websiteUri") or "",
            ]
        )
    return rows

