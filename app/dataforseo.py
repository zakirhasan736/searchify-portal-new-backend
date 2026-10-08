"""Optional DataForSEO organic SERP — competitor titles for the work-queue writer.

If DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are empty, returns [] and Sol still writes from the scraped page.
"""

from __future__ import annotations

import httpx

from app import config

LIVE_ORGANIC = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"


def configured() -> bool:
    return bool(config.DATAFORSEO_LOGIN and config.DATAFORSEO_PASSWORD)


def competitor_serp(
    keyword: str,
    *,
    location_code: int = 2840,
    location_name: str = "",
    language_code: str = "en",
    depth: int = 10,
) -> list[dict]:
    query = (keyword or "").strip()
    if not query or not configured():
        return []
    where = {"location_name": location_name} if location_name else {"location_code": location_code}
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
            url = str(item.get("url") or "").strip()
            desc = str(item.get("description") or "").strip()
            if not title:
                continue
            out.append({"title": title[:120], "url": url[:300], "description": desc[:200], "rank": item.get("rank_group") or item.get("rank_absolute")})
            if len(out) >= 8:
                break
        return out
    except Exception:  # noqa: BLE001
        return []
