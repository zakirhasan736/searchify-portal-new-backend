"""Keep reports, Search Console, Analytics, and Speed Index on one website."""

from urllib.parse import urlparse

UNSCOPED_KINDS = {
    "profile",
    "business-profile",
    "keyword-group",
    "keyword-list",
    "keyword-share",
    "website-keyword-list",
    "competitor-list",
    "workspace-controls",
    "workspace-backup",
}


def host_key(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("sc-domain:"):
        return text.split(":", 1)[1].strip("/").lower().removeprefix("www.")
    raw = text if "://" in text else f"https://{text}"
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def payload_host(payload: dict | None) -> str:
    data = payload or {}
    for key in ("siteHost", "host", "siteUrl", "gscSiteUrl", "home", "url", "finalUrl"):
        found = host_key(str(data.get(key) or ""))
        if found:
            return found
    return ""


def matches_site(kind: str, payload: dict | None, wanted: str) -> bool:
    """True when this record may be shown for the selected website."""
    wanted = host_key(wanted)
    if not wanted or kind in UNSCOPED_KINDS:
        return True
    found = payload_host(payload)
    if found:
        return found == wanted
    source = str((payload or {}).get("source") or "")
    if source.startswith("google") or kind in {
        "organic-search",
        "traffic-analytics",
        "top-pages",
        "site-audit",
        "on-page-seo",
        "ga4-landing-pages",
        "site-scan",
        "backlinks",
        "keyword-research",
    }:
        return False
    return True
