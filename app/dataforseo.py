"""Optional DataForSEO organic SERP — competitor titles for the work-queue writer.

If DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are empty, returns [] and Sol still writes from the scraped page.
"""

from __future__ import annotations

import re

import httpx

from app import config

LIVE_ORGANIC = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"


def configured() -> bool:
    return bool(config.DATAFORSEO_LOGIN and config.DATAFORSEO_PASSWORD)


def _public_url(item: dict) -> str:
    """Organic url. Google sometimes wraps the listing, with the real site only in the breadcrumb."""
    url = str(item.get("url") or "").strip()
    domain = str(item.get("domain") or "").lower()
    wrapped = "google." in domain or "google.com/goto" in url or url.startswith("https://google.")
    crumb = str(item.get("breadcrumb") or "").strip()
    if wrapped and crumb:
        match = re.search(r"https?://[^\s›>]+", crumb)
        if match:
            return match.group(0).strip()
        hostish = crumb.split("›")[0].split(">")[0].strip()
        if hostish and "." in hostish and " " not in hostish:
            return f"https://{hostish.lstrip('/')}"
    return url


def competitor_serp(
    keyword: str,
    *,
    location_name: str = "",
    language_code: str = "en",
    depth: int = 10,
) -> list[dict]:
    """Google organic results for one search. Without a resolved location there is no request."""
    query = (keyword or "").strip()
    if not query or not location_name or not configured():
        return []
    where = {"location_name": location_name}
    try:
        with httpx.Client(timeout=25.0) as client:
            response = client.post(
                LIVE_ORGANIC,
                auth=(config.DATAFORSEO_LOGIN, config.DATAFORSEO_PASSWORD),
                json=[
                    {
                        "keyword": query[:200],
                        **where,
                        "language_code": language_code,
                        "depth": min(max(depth, 5), 20),
                    }
                ],
            )
        if response.status_code >= 400:
            return []
        data = response.json()
        tasks = data.get("tasks") or []
        if not tasks:
            return []
        result = (tasks[0] or {}).get("result") or []
        if not result:
            return []
        items = result[0].get("items") or []
        out = []
        for item in items:
            if str(item.get("type") or "") != "organic":
                continue
            title = str(item.get("title") or "").strip()
            url = _public_url(item)
            desc = str(item.get("description") or "").strip()
            if not title:
                continue
            out.append({"title": title[:120], "url": url[:300], "description": desc[:200], "rank": item.get("rank_group") or item.get("rank_absolute")})
            if len(out) >= 8:
                break
        return out
    except Exception:  # noqa: BLE001
        return []
