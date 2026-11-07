"""Live rank tracking for tracked keywords.

Each check asks Google through the DataForSEO SERP API (live, advanced) for one keyword, one device
and one location, finds this website in the organic results, notes the SERP features on the page and
which ones the website appears in, and stores the result as history. Nothing is estimated.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import KeywordRankCheck, TrackedKeyword, User
from app.site_scope import host_key

log = logging.getLogger("searchify.ranks")

SERP_PATH = "/serp/google/organic/live/advanced"
DEPTH = 100
DEVICES = ("desktop", "mobile")
DAILY = timedelta(hours=23)
MANUAL_COOLDOWN = timedelta(minutes=30)
HISTORY_DAYS = 120

FEATURE_LABELS = {
    "featured_snippet": "Featured snippet",
    "local_pack": "Local map pack",
    "map": "Map",
    "people_also_ask": "People also ask",
    "ai_overview": "AI overview",
    "images": "Images",
    "video": "Videos",
    "top_stories": "Top stories",
    "knowledge_graph": "Knowledge panel",
    "shopping": "Shopping",
    "popular_products": "Popular products",
    "twitter": "X posts",
    "perspectives": "Perspectives",
    "discussions_and_forums": "Forums",
    "related_searches": "Related searches",
    "people_also_search": "People also search",
    "paid": "Ads",
}
SKIP_OWNERSHIP = {"organic", "paid", "related_searches", "people_also_search"}


def city_location(place) -> str:
    """DataForSEO location name for a city ("Calgary,Alberta,Canada"), or "" when it cannot be named."""
    city, region, country = (getattr(place, k, "") or "" for k in ("city", "region", "country"))
    if city and region and country:
        return f"{city},{region},{country}"
    return ""


def _domain_match(value: str, host: str) -> bool:
    found = host_key(value or "")
    return bool(found) and (found == host or found.endswith("." + host))


def parse_serp(result: dict, host: str) -> dict:
    """Our organic position, URL, SERP features present / owned, and the top three organic results."""
    items = (result or {}).get("items") or []
    position, url, top = None, "", []
    features, owned = [], []
    for item in items:
        kind = str(item.get("type") or "")
        if kind == "organic":
            domain = item.get("domain") or item.get("url") or ""
            if len(top) < 3:
                top.append({
                    "position": item.get("rank_group"),
                    "domain": host_key(domain),
                    "url": item.get("url") or "",
                    "title": (item.get("title") or "")[:160],
                })
            if position is None and _domain_match(domain, host):
                position = item.get("rank_group")
                url = item.get("url") or ""
            continue
        if kind and kind not in features:
            features.append(kind)
        if kind and kind not in SKIP_OWNERSHIP and kind not in owned and host in json.dumps(item, default=str).lower():
            owned.append(kind)
    return {"position": position, "url": url, "features": features, "owned": owned, "top": top}


def serp_check(keyword: str, host: str, *, location_name: str, language: str, device: str) -> tuple[dict, float]:
    from app import research

    payload = {
        "keyword": keyword[:200],
        "location_name": location_name,
        "language_code": language or "en",
        "device": device,
        "depth": DEPTH,
    }
    if device == "mobile":
        payload["os"] = "android"
    result, cost = research._call("POST", SERP_PATH, [payload], timeout=90)
    return parse_serp((result or [{}])[0] or {}, host), cost


def _place(db: Session, user: User, host: str):
    from app import research

    place, market = research._place_for(db, user, site=host, tool="keywords")
    return place, place.language or market.get("language") or "en"


def check_keywords(
    db: Session,
    user: User,
    host: str,
    rows: list[TrackedKeyword],
    *,
    devices: tuple[str, ...] = DEVICES,
    scheduled: bool = False,
) -> dict:
    """Run live checks for the given tracked keywords and store them. City first, country if the city
    is not a DataForSEO location."""
    from app import research

    if not rows:
        return {"checked": 0, "failed": 0, "location": ""}
    place, language = _place(db, user, host)
    city = city_location(place)
    jobs = [(row, device) for row in rows for device in devices]
    if not scheduled:
        research._budget(db, user, len(jobs))
    where = {"name": city or place.country}

    def run(job):
        row, device = job
        try:
            return job, *serp_check(row.keyword, host, location_name=where["name"], language=language, device=device), ""
        except research.ResearchError as exc:
            if where["name"] != place.country and "location" in str(getattr(exc, "technical", "") or exc).lower():
                where["name"] = place.country
                try:
                    return job, *serp_check(row.keyword, host, location_name=place.country, language=language, device=device), ""
                except research.ResearchError as again:
                    return job, None, 0.0, str(again)
            return job, None, 0.0, str(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, jobs))
    cost, ok, failed, errors = 0.0, 0, 0, []
    now = datetime.utcnow()
    for (row, device), parsed, spent, error in results:
        cost += spent or 0.0
        if parsed is None:
            failed += 1
            if error and error not in errors:
                errors.append(error)
            continue
        ok += 1
        db.add(KeywordRankCheck(
            customer_id=user.id,
            tracked_keyword_id=row.id,
            host=host,
            keyword=row.keyword,
            device=device,
            location=where["name"],
            position=parsed["position"],
            url=parsed["url"],
            features=parsed["features"],
            owned=parsed["owned"],
            top=parsed["top"],
            checked_at=now,
        ))
    db.commit()
    research._spend(db, user, ok + failed, cost)
    return {"checked": ok, "failed": failed, "location": where["name"], "errors": errors[:3], "checkedAt": now.isoformat(timespec="seconds")}


def last_check_at(db: Session, user_id: int, host: str) -> datetime | None:
    row = (
        db.query(KeywordRankCheck.checked_at)
        .filter(KeywordRankCheck.customer_id == user_id, KeywordRankCheck.host == host)
        .order_by(KeywordRankCheck.checked_at.desc())
        .first()
    )
    return row[0] if row else None


def check_site(db: Session, user: User, host: str, *, keyword: str = "", scheduled: bool = False) -> dict:
    """Check every tracked keyword of a website (or just one). Manual checks wait 30 minutes between runs."""
    from app import keyword_tracking, research

    host = host_key(host)
    rows = keyword_tracking.tracked_rows(db, user.id, host)
    if keyword:
        term = keyword_tracking.clean_keyword(keyword)
        rows = [row for row in rows if row.keyword == term]
    elif not scheduled:
        last = last_check_at(db, user.id, host)
        if last and datetime.utcnow() - last < MANUAL_COOLDOWN:
            wait = int((MANUAL_COOLDOWN - (datetime.utcnow() - last)).total_seconds() // 60) + 1
            raise research.ResearchError(f"Ranks were checked a moment ago. Check again in {wait} minutes.", code="rank_cooldown")
    return check_keywords(db, user, host, rows, scheduled=scheduled)


def rank_history(db: Session, user_id: int, host: str) -> dict[str, dict]:
    """keyword → {desktop: {...latest, previous, history[]}, mobile: {...}} for the last 120 days."""
    since = datetime.utcnow() - timedelta(days=HISTORY_DAYS)
    rows = (
        db.query(KeywordRankCheck)
        .filter(KeywordRankCheck.customer_id == user_id, KeywordRankCheck.host == host, KeywordRankCheck.checked_at >= since)
        .order_by(KeywordRankCheck.checked_at.asc())
        .all()
    )
    out: dict[str, dict] = {}
    for row in rows:
        slot = out.setdefault(row.keyword, {}).setdefault(row.device, {"history": []})
        day = row.checked_at.date().isoformat()
        point = {"date": day, "at": row.checked_at.isoformat(timespec="seconds"), "position": row.position}
        # One point per day: the last check of that day wins.
        if slot["history"] and slot["history"][-1]["date"] == day:
            slot["history"][-1] = point
        else:
            slot["history"].append(point)
        slot["latest"] = {
            "position": row.position,
            "url": row.url,
            "features": [{"type": f, "label": FEATURE_LABELS.get(f, f.replace("_", " ").capitalize()), "owned": f in (row.owned or [])} for f in row.features or []],
            "top": row.top or [],
            "location": row.location,
            "checkedAt": row.checked_at.isoformat(timespec="seconds"),
        }
    for devices in out.values():
        for slot in devices.values():
            hist = slot["history"]
            slot["previous"] = hist[-2]["position"] if len(hist) > 1 else None
            first = next((p["position"] for p in hist if p["position"] is not None), None)
            slot["first"] = first
            best = [p["position"] for p in hist if p["position"] is not None]
            slot["best"] = min(best) if best else None
    return out


def run_due_rank_checks() -> dict:
    """Daily live checks for every website that has tracked keywords (called by the background loop)."""
    from app import research
    from app.auto_sync import _scan_controls
    from app.database import SessionLocal

    if not research.configured():
        return {"sites": 0}
    db = SessionLocal()
    done = 0
    try:
        pairs = db.query(TrackedKeyword.customer_id, TrackedKeyword.host).distinct().all()
        for customer_id, host in pairs:
            last = last_check_at(db, customer_id, host)
            if last and datetime.utcnow() - last < DAILY:
                continue
            if _scan_controls(db, customer_id)["scansPaused"]:
                continue
            user = db.get(User, customer_id)
            if user is None:
                continue
            try:
                check_site(db, user, host, scheduled=True)
                done += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                log.warning("rank check failed user=%s host=%s err=%s", customer_id, host, exc)
    finally:
        db.close()
    return {"sites": done}
