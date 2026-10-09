"""Match a website to its Search Console property and GA4 property.

A match is only automatic when exactly one property is the best fit. Otherwise the candidates
are returned so the owner chooses. Nothing here calls Google; the caller passes the lists.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

UNUSABLE = {"siteUnverifiedUser"}


def bare_host(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw.startswith("sc-domain:"):
        return raw.split(":", 1)[1].strip("/").removeprefix("www.")
    if not re.match(r"^[a-z]+://", raw):
        raw = f"https://{raw}"
    return (urlparse(raw).hostname or "").removeprefix("www.")


def _gsc_tier(site_url: str, wanted_host: str, wanted_scheme: str, wanted_www: bool) -> int | None:
    """Lower is better. None means the property does not cover the site."""
    if site_url.startswith("sc-domain:"):
        domain = site_url.split(":", 1)[1].strip("/").lower()
        if wanted_host == domain or wanted_host.endswith(f".{domain}"):
            return 0
        return None
    parsed = urlparse(site_url)
    host = (parsed.hostname or "").lower()
    if host.removeprefix("www.") != wanted_host:
        return None
    if (parsed.path or "/") not in {"", "/"}:
        return 4
    exact = parsed.scheme == wanted_scheme and host.startswith("www.") == wanted_www
    if exact:
        return 1
    if parsed.scheme == "https":
        return 2
    return 3


def match_gsc(site: str, properties: list[dict]) -> dict:
    """{status: matched|ambiguous|none, selected, candidates}."""
    raw = (site or "").strip()
    if not raw:
        return {"status": "none", "selected": "", "candidates": [], "reason": "No website address to match."}
    url = raw if re.match(r"^[a-z]+://", raw, re.I) else f"https://{raw}"
    parsed = urlparse(url)
    wanted_host = (parsed.hostname or "").lower().removeprefix("www.")
    wanted_www = (parsed.hostname or "").lower().startswith("www.")
    scored = []
    for prop in properties or []:
        site_url = prop.get("siteUrl") or ""
        if not site_url or prop.get("permissionLevel") in UNUSABLE:
            continue
        tier = _gsc_tier(site_url, wanted_host, parsed.scheme or "https", wanted_www)
        if tier is not None:
            scored.append((tier, site_url))
    if not scored:
        return {"status": "none", "selected": "", "candidates": [],
                "reason": f"This Google account has no Search Console property for {wanted_host}."}
    scored.sort()
    best = [s for t, s in scored if t == scored[0][0]]
    candidates = [s for _, s in scored]
    if len(best) == 1:
        return {"status": "matched", "selected": best[0], "candidates": candidates}
    return {"status": "ambiguous", "selected": "", "candidates": candidates,
            "reason": "More than one Search Console property fits this website. Choose one."}


def match_ga4(site: str, properties: list[dict]) -> dict:
    """Properties carry `streams` (list of default URIs) when they could be read."""
    wanted = bare_host(site)
    if not wanted:
        return {"status": "none", "selected": "", "candidates": [], "reason": "No website address to match."}
    hits = [p for p in properties or [] if any(bare_host(uri) == wanted for uri in p.get("streams") or [])]
    if len(hits) == 1:
        return {"status": "matched", "selected": hits[0]["propertyId"], "selectedName": hits[0].get("displayName") or "",
                "candidates": [hits[0]["propertyId"]]}
    if len(hits) > 1:
        return {"status": "ambiguous", "selected": "", "candidates": [p["propertyId"] for p in hits],
                "reason": "More than one Analytics property has a web stream for this website. Choose one."}
    return {"status": "none", "selected": "", "candidates": [p["propertyId"] for p in properties or []],
            "reason": f"No Analytics web stream points at {wanted}. Choose the property yourself."}
