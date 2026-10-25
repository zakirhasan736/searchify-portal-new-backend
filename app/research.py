"""Searchify SEO research for Keywords, Backlinks, and AI visibility.

Every result is stored per website so the same lookup is not paid for twice inside its window.
A daily call budget per customer keeps a large site from running up the bill.
Nothing is invented: an empty provider answer is returned as empty.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app import config, llm_requests, locations, markets, quotas
from app.concept_reads import backlink_concepts, business_offers, keyword_concepts, match_keyword, site_concept, offer_fit, seed_phrase, visibility_reading
from app.models import FeatureRecord, User

log = logging.getLogger("searchify.research")

BASE = "https://api.dataforseo.com/v3"
DAILY_CALLS = {"starter": 40, "growth": 150, "agency": 500, "scale": 800}
WINDOWS = {"keywords": timedelta(hours=24), "backlinks": timedelta(hours=24), "visibility": timedelta(days=7), "models": timedelta(days=7), "audit": timedelta(days=7)}
AUDIT_PAGES = {"starter": 100, "growth": 300, "agency": 1000, "scale": 1000}
AUDIT_STALE = timedelta(hours=3)
ENGINES = llm_requests.ENGINES
# Bump when keyword seeding changes so suggestions built from old seeds refresh once.
KEYWORDS_QUALITY = 4
PROVIDER_ERRORS = {
    40104: "Searchify SEO is not fully set up yet. Ask an admin to finish Searchify SEO setup.",
    40200: "Searchify SEO research credit is empty. Top up Searchify SEO to run live research again.",
    40210: "Searchify SEO research credit is too low for this request. Top up Searchify SEO and try again.",
    40100: "Searchify SEO could not authenticate. Ask an admin to check the Searchify SEO connection.",
    40204: "This Searchify SEO research feature is not active. Ask an admin to enable it.",
}


class ResearchError(Exception):
    def __init__(self, message: str, *, code: str = "research_error"):
        super().__init__(message)
        self.code = code


def configured() -> bool:
    return bool(config.DATAFORSEO_LOGIN and config.DATAFORSEO_PASSWORD)


def _provider_error(code: int, fallback: str) -> ResearchError:
    message = PROVIDER_ERRORS.get(code) or fallback
    if code in (40200, 40210):
        _STATUS.clear()
        return ResearchError(message, code="provider_balance")
    if code in (40100, 40104):
        return ResearchError(message, code="provider_auth")
    if code == 40204:
        return ResearchError(message, code="provider_api_disabled")
    return ResearchError(message, code="provider_error")


def host_of(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"^[a-z]+://", "", raw).split("/")[0].split("?")[0]
    return raw.removeprefix("www.")


def location_for(text: str = "", reach: str = "") -> tuple[str, str]:
    """(Searchify SEO location_name, ISO). Raises locations.LocationError instead of defaulting."""
    place = locations.resolve(text, reach=reach)
    return place.country, place.iso


def _record(db: Session, user_id: int, kind: str, title: str) -> FeatureRecord | None:
    return (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind, FeatureRecord.title == title)
        .order_by(FeatureRecord.id.desc())
        .first()
    )


def _save(db: Session, user_id: int, kind: str, title: str, payload: dict) -> None:
    row = _record(db, user_id, kind, title)
    if row is None:
        db.add(FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload, status="stored"))
    else:
        row.payload = payload
        row.status = "stored"
    db.commit()


def cached(db: Session, user_id: int, kind: str, title: str, window: timedelta) -> dict | None:
    row = _record(db, user_id, kind, title)
    payload = dict((row.payload if row else {}) or {})
    stamp = payload.get("fetchedAt")
    if not stamp:
        return None
    try:
        fresh = datetime.utcnow() - datetime.fromisoformat(stamp) < window
    except ValueError:
        return None
    return payload if fresh else None


def _budget(db: Session, user: User, calls: int) -> None:
    today = datetime.utcnow().date().isoformat()
    row = _record(db, user.id, "dfs-usage", "DataForSEO usage")
    usage = dict((row.payload if row else {}) or {})
    if usage.get("date") != today:
        usage = {"date": today, "calls": 0, "cost": 0.0}
    plan = (getattr(user, "plan", None) or "starter").lower()
    limit = DAILY_CALLS["scale"] if user.role == "ROLE_ADMIN" else DAILY_CALLS.get(plan, DAILY_CALLS["starter"])
    if usage["calls"] + calls > limit:
        raise ResearchError(
            f"Today's research limit for this plan is used ({limit} lookups). It resets tomorrow.",
            code="daily_limit",
        )


def _spend(db: Session, user: User, calls: int, cost: float) -> None:
    today = datetime.utcnow().date().isoformat()
    row = _record(db, user.id, "dfs-usage", "DataForSEO usage")
    usage = dict((row.payload if row else {}) or {})
    if usage.get("date") != today:
        usage = {"date": today, "calls": 0, "cost": 0.0}
    usage["calls"] = int(usage.get("calls") or 0) + calls
    usage["cost"] = round(float(usage.get("cost") or 0) + float(cost or 0), 4)
    _save(db, user.id, "dfs-usage", "DataForSEO usage", usage)


def _call(method: str, path: str, payload: list | None = None, timeout: float = 60.0) -> tuple[list, float]:
    """Returns (result list of the first task, cost). Raises ResearchError with a plain message."""
    task, cost = _task(method, path, payload, timeout)
    return list(task.get("result") or []), cost


def _task(method: str, path: str, payload: list | None = None, timeout: float = 60.0, ok: tuple[int, ...] = (20000,)) -> tuple[dict, float]:
    if not configured():
        raise ResearchError(
            "Searchify SEO is not connected. Ask an admin to connect Searchify SEO in the backend settings.",
            code="provider_not_configured",
        )
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method,
                f"{BASE}{path}",
                auth=(config.DATAFORSEO_LOGIN, config.DATAFORSEO_PASSWORD),
                json=payload,
            )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ResearchError(f"Searchify SEO could not be reached. {str(exc)[:120]}", code="provider_unreachable") from exc
    code = int(data.get("status_code") or 0)
    if code != 20000:
        raise _provider_error(code, f"Searchify SEO: {data.get('status_message') or 'request failed'}")
    task = (data.get("tasks") or [{}])[0] or {}
    task_code = int(task.get("status_code") or 0)
    if task_code and task_code not in ok:
        raise _provider_error(task_code, f"Searchify SEO: {task.get('status_message') or 'task failed'}")
    return task, float(data.get("cost") or 0)


_STATUS: dict = {}
STATUS_TTL = timedelta(minutes=10)


def status() -> dict:
    """Account check through the free user_data endpoint, reused for ten minutes."""
    if not configured():
        return {"connected": False, "ready": False, "message": "Ask an admin to connect Searchify SEO in the backend settings."}
    if _STATUS.get("at") and datetime.utcnow() - _STATUS["at"] < STATUS_TTL:
        return {**_STATUS["value"], "cached": True}
    try:
        result, _ = _call("GET", "/appendix/user_data", None, timeout=20)
    except ResearchError as exc:
        return {"connected": True, "ready": False, "message": str(exc)}
    info = (result or [{}])[0] or {}
    balance = (info.get("money") or {}).get("balance")
    try:
        funds = float(balance) if balance is not None else None
    except (TypeError, ValueError):
        funds = None
    if funds is not None and funds <= 0:
        value = {
            "connected": True,
            "ready": False,
            "balance": funds,
            "code": "provider_balance",
            "message": "Searchify SEO research credit is empty. Top up Searchify SEO before live keywords, backlinks, or AI visibility can run.",
        }
    elif funds is not None and funds < 1:
        value = {
            "connected": True,
            "ready": True,
            "balance": funds,
            "code": "provider_balance_low",
            "message": f"Searchify SEO is connected, but research credit is low (${funds:.2f}). Top up soon so live research does not stop mid-check.",
        }
    else:
        value = {"connected": True, "ready": True, "balance": balance, "message": "Searchify SEO is connected."}
    _STATUS.update(at=datetime.utcnow(), value=value)
    return value


def _kw_row(item: dict) -> dict:
    data = item.get("keyword_data") or item
    info = data.get("keyword_info") or {}
    props = data.get("keyword_properties") or {}
    intent = (data.get("search_intent_info") or {}).get("main_intent") or ""
    return {
        "keyword": data.get("keyword") or "",
        "volume": info.get("search_volume"),
        "cpc": info.get("cpc"),
        "difficulty": props.get("keyword_difficulty"),
        "intent": intent,
    }


def _place_for(db: Session, user: User, *, site: str, country: str = "", reach: str = "", tool: str = "") -> tuple[locations.Location, dict]:
    """Resolve the shared market, or an explicit override from the request."""
    if (country or "").strip() or db is None:
        place = locations.resolve(country, reach=reach)
        ctx = {"source": "request", "countryIso": place.iso, "language": place.language, "version": 0}
        return place, ctx
    ctx = markets.get_market_context(db, user, site=site, tool_name=tool)
    place = locations.Location(
        country=(ctx.get("place") or {}).get("country") or ctx.get("countryName") or "",
        iso=ctx.get("countryIso") or "",
        language=ctx.get("language") or "en",
        city=ctx.get("city") or "",
        region=ctx.get("region") or "",
        worldwide=bool(ctx.get("worldwide")),
        source=ctx.get("source") or "market",
    )
    return place, ctx


def _rank_key(row: dict) -> tuple:
    """Best organic position first, then higher volume."""
    pos = row.get("position")
    vol = row.get("volume")
    return (pos if isinstance(pos, int) else 10**6, -(vol if isinstance(vol, (int, float)) else -1))


def _idea_seeds(
    clean: list[str],
    ranked: list[dict],
    limit: int = 3,
    offers: list[str] | None = None,
) -> list[str]:
    """Starting phrases for DataForSEO keyword suggestions, taken only from this site's own data:
    the offers its pages name (site read), the terms the owner tracks, and keywords it already ranks for.
    Nothing is added or preset by Searchify."""
    seeds: list[str] = []

    def add(term: str) -> bool:
        text = seed_phrase(term)
        if text and text.lower() not in {item.lower() for item in seeds}:
            seeds.append(text)
        return len(seeds) >= limit

    named = [offer for offer in (offers or []) if str(offer).strip()]
    if named:
        for offer in named:
            if add(offer):
                return seeds
        for term in clean:
            if offer_fit(term, named)[0] >= 0.34 and add(term):
                return seeds
        for row in sorted(ranked, key=_rank_key):
            kw = (row.get("keyword") or "").strip()
            if kw and offer_fit(kw, named)[0] >= 0.34 and add(kw):
                return seeds
        return seeds
    for term in clean:
        if add(term):
            return seeds
    for row in sorted(ranked, key=_rank_key):
        if add((row.get("keyword") or "").strip()):
            return seeds
    return seeds


def _stored_keywords(db: Session, user_id: int, host: str) -> dict | None:
    if db is None:
        return None
    rows = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "dfs-keywords")
        .order_by(FeatureRecord.id.desc())
        .limit(20)
        .all()
    )
    for row in rows:
        payload = dict(row.payload or {})
        if host_of(str(payload.get("host") or "")) == host:
            return payload
    return None


def keywords(db: Session, user: User, *, site: str, terms: list[str], country: str = "", reach: str = "", force: bool = False, stored_only: bool = False) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.", code="site_required")
    if stored_only:
        saved = _stored_keywords(db, user.id, host)
        if not saved:
            return {"host": host, "state": "none", "ideas": [], "ranked": [], "cached": True, "storedOnly": True}
        return {**saved, "cached": True, "storedOnly": True}
    place, market = _place_for(db, user, site=site, country=country, reach=reach, tool="keywords")
    location, language = place.country, place.language or market.get("language") or "en"
    clean = [t.strip()[:80] for t in dict.fromkeys(t.strip().lower() for t in terms or [] if t and t.strip())][:50]
    term_limit = quotas.tracked_keyword_limit(db, user)
    dropped = len(clean) - term_limit if term_limit is not None and len(clean) > term_limit else 0
    if dropped:
        clean = clean[:term_limit]
    title = f"Keywords {host} {place.iso or location} {language}"
    saved = None if force else cached(db, user.id, "dfs-keywords", title, WINDOWS["keywords"])
    offers = business_offers(db, user.id, host)
    if saved and set(clean) <= set(saved.get("terms") or []) and saved.get("qualityVersion") == KEYWORDS_QUALITY:
        concepts = keyword_concepts(saved, offers)
        return {**saved, "concepts": concepts["rows"], "conceptNote": concepts["note"], "cached": True, "termsDropped": dropped}
    quotas.check(db, user, "keywords")
    _budget(db, user, 5)
    cost = 0.0
    ranked_payload = {
        "target": host,
        "location_name": location,
        "language_code": language,
        "limit": 200,
        "item_types": ["organic"],
        "filters": ["ranked_serp_element.serp_item.rank_group", "<=", 100],
        "order_by": [
            "ranked_serp_element.serp_item.rank_group,asc",
            "keyword_data.keyword_info.search_volume,desc",
        ],
    }
    try:
        ranked_res, c = _call("POST", "/dataforseo_labs/google/ranked_keywords/live", [ranked_payload])
    except ResearchError:
        # Older account schemas may reject item_types/filters — retry with the core fields.
        ranked_payload = {
            "target": host,
            "location_name": location,
            "language_code": language,
            "limit": 200,
            "order_by": ["keyword_data.keyword_info.search_volume,desc"],
        }
        ranked_res, c = _call("POST", "/dataforseo_labs/google/ranked_keywords/live", [ranked_payload])
    cost += c
    best: dict[str, dict] = {}
    for item in ((ranked_res or [{}])[0] or {}).get("items") or []:
        row = _kw_row(item)
        serp = (item.get("ranked_serp_element") or {}).get("serp_item") or {}
        if (serp.get("type") or "organic") not in ("", "organic"):
            continue
        changes = serp.get("rank_changes") or {}
        row.update({
            "position": serp.get("rank_group") or serp.get("rank_absolute"),
            "previous": changes.get("previous_rank_absolute"),
            "isNew": bool(changes.get("is_new")),
            "url": serp.get("url") or "",
            "page": serp.get("relative_url") or "",
        })
        if not row["keyword"]:
            continue
        key = row["keyword"].lower()
        held = best.get(key)
        if held is None:
            best[key] = row
        elif (row["position"] or 10**6) < (held["position"] or 10**6):
            row["otherUrls"] = [u for u in [held["url"], *held.get("otherUrls", [])] if u]
            best[key] = row
        else:
            if row.get("url"):
                held.setdefault("otherUrls", []).append(row["url"])
    ranked = sorted(best.values(), key=_rank_key)
    for row in ranked:
        row["source"] = "Searchify SEO ranked keywords (estimated Google organic position)"
    by_term = {row["keyword"].lower(): row for row in ranked}

    tracked = []
    if clean:
        overview_res, c = _call(
            "POST",
            "/dataforseo_labs/google/keyword_overview/live",
            [{"keywords": clean, "location_name": location, "language_code": language, "include_serp_info": True}],
        )
        cost += c
        found = {}
        for item in ((overview_res or [{}])[0] or {}).get("items") or []:
            row = _kw_row(item)
            serp = item.get("serp_info") or {}
            if serp.get("se_results_count") is not None:
                row["results"] = serp.get("se_results_count")
            found[row["keyword"].lower()] = row
        for term in clean:
            base = found.get(term.lower()) or {"keyword": term, "volume": None, "difficulty": None, "intent": ""}
            rank = by_term.get(term.lower()) or {}
            tracked.append({
                **base,
                "keyword": term,
                "position": rank.get("position"),
                "previous": rank.get("previous"),
                "page": rank.get("page") or "",
                "url": rank.get("url") or "",
                "source": "Searchify SEO keyword overview + ranked keywords" if term.lower() in found or rank else "Not in Searchify SEO keyword data",
            })

    # Keyword ideas come from DataForSEO reading this website itself (Keywords For Site): no seed
    # phrases and nothing preset by Searchify. Ordered by relevance to the site's own content.
    ideas = []
    seeds: list[str] = []
    known = {t.lower() for t in clean} | set(by_term)
    site_payload = {
        "target": host,
        "location_name": location,
        "language_code": language,
        "include_subdomains": True,
        "limit": 200,
        "filters": ["keyword_info.search_volume", ">", 0],
        "order_by": ["relevance,desc"],
    }
    try:
        site_res, c = _call("POST", "/dataforseo_labs/google/keywords_for_site/live", [site_payload])
    except ResearchError:
        # Some accounts reject filters/order_by on this endpoint; retry with the core fields.
        site_res, c = _call(
            "POST",
            "/dataforseo_labs/google/keywords_for_site/live",
            [{k: v for k, v in site_payload.items() if k not in {"filters", "order_by"}}],
        )
    cost += c
    idea_calls = 1
    concept = site_concept(db, user.id, host)
    understood = bool(concept["offers"] or concept["category"] or concept["pages"])
    for position, item in enumerate(((site_res or [{}])[0] or {}).get("items") or []):
        row = _kw_row(item)
        key = (row.get("keyword") or "").lower()
        if not key or key in known:
            continue
        known.add(key)
        intent = row.get("intent") or ""
        where = match_keyword(row["keyword"], concept) if understood else {"fit": 0.0, "concept": "", "page": "", "pageTarget": ""}
        ideas.append({
            **row,
            "source": f"Searchify SEO keywords for site · DataForSEO read of {host}",
            # Grouping comes from DataForSEO's own search-intent label, not a Searchify seed.
            "seed": intent,
            # Which part of the business this search belongs to and which page should rank for it,
            # from the AI read of every page of the site.
            "concept": where["concept"],
            "page": where["page"],
            "pageTarget": where["pageTarget"],
            "_fit": where["fit"],
            "_order": position,
        })
    if understood:
        # Keep what this business actually sells; DataForSEO relevance order breaks ties.
        on_business = [row for row in ideas if row["_fit"] >= 0.34]
        if on_business:
            ideas = on_business
        ideas.sort(key=lambda row: (-round(row["_fit"], 1), row["_order"]))
    for row in ideas:
        row.pop("_fit", None)
        row.pop("_order", None)
    paid_calls = 1 + bool(clean) + idea_calls
    _spend(db, user, paid_calls, cost)
    quotas.consume(db, user, "keywords")
    idea_limit = quotas.keyword_idea_limit(db, user)
    payload = {
        "host": host,
        "location": location,
        "language": language,
        "place": place.to_dict(),
        "market": {
            "countryIso": place.iso,
            "language": language,
            "source": market.get("source"),
            "version": market.get("version"),
            "scope": "country",
        },
        "terms": clean,
        "ranked": ranked,
        "tracked": tracked,
        "ideas": ideas[:idea_limit],
        "seeds": seeds,
        "termLimit": term_limit,
        "qualityVersion": KEYWORDS_QUALITY,
        "provider": "dataforseo",
        "dataKind": "keywords",
        "source": "Searchify SEO",
        "metricsNote": (
            "Volume, CPC, and difficulty are DataForSEO estimates. "
            "Position is an estimated Google organic rank from DataForSEO Labs for this host — "
            "not a Search Console impression or click. Missing values stay null (not zero)."
        ),
        "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
    }
    concepts = keyword_concepts(payload, offers)
    payload["concepts"] = concepts["rows"]
    payload["conceptNote"] = concepts["note"]
    _save(db, user.id, "dfs-keywords", title, payload)
    return {**payload, "cached": False, "termsDropped": dropped}


def _link_domain(item: dict) -> str:
    return (item.get("domain_from") or "").lower().removeprefix("www.")


def _link_quality(item: dict) -> tuple:
    """Prefer live, dofollow, stronger referring domains, lower spam."""
    lost = 1 if item.get("is_lost") else 0
    follow = 0 if item.get("dofollow") else 1
    domain_rank = item.get("domain_from_rank")
    link_rank = item.get("rank")
    spam = item.get("backlink_spam_score")
    return (
        lost,
        follow,
        -(domain_rank if isinstance(domain_rank, (int, float)) else -1),
        -(link_rank if isinstance(link_rank, (int, float)) else -1),
        spam if isinstance(spam, (int, float)) else 999,
    )


def _map_backlink(item: dict) -> dict:
    state = "Lost" if item.get("is_lost") else "New" if item.get("is_new") else "Active"
    domain = item.get("domain_from") or ""
    return {
        "domain": domain.removeprefix("www.") if domain.lower().startswith("www.") else domain,
        "source": item.get("url_from") or "",
        "target": item.get("url_to") or "",
        "anchor": item.get("anchor") or "",
        "follow": "Follow" if item.get("dofollow") else "Nofollow",
        "state": state,
        "rank": item.get("domain_from_rank") if item.get("domain_from_rank") is not None else item.get("rank"),
        "linkRank": item.get("rank"),
        "domainRank": item.get("domain_from_rank"),
        "pageRank": item.get("page_from_rank"),
        "spamScore": item.get("backlink_spam_score"),
        "pageTitle": item.get("page_from_title") or "",
        "firstSeen": str(item.get("first_seen") or "")[:10] or None,
        "lastSeen": str(item.get("last_seen") or "")[:10] or None,
        "lostDate": str(item.get("lost_date") or item.get("date_lost") or "")[:10] or None,
        "seen": f"First seen {str(item.get('first_seen') or '')[:10]} · last seen {str(item.get('last_seen') or '')[:10]}",
    }


def _collect_backlinks(items: list, *, prefer: dict[str, dict] | None = None) -> dict[str, dict]:
    """One best link per referring domain (www stripped)."""
    best = dict(prefer or {})
    for item in items or []:
        key = _link_domain(item)
        if not key or item.get("is_broken"):
            continue
        spam = item.get("backlink_spam_score")
        if isinstance(spam, (int, float)) and spam >= 60 and not item.get("is_lost"):
            # Keep high-spam live links out of the “best” list; lost still show for review.
            continue
        held = best.get(key)
        if held is None or _link_quality(item) < _link_quality(held["_raw"]):
            mapped = _map_backlink(item)
            mapped["_raw"] = item
            best[key] = mapped
    return best


def backlinks(db: Session, user: User, *, site: str, force: bool = False, stored_only: bool = False) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.", code="site_required")
    title = f"Backlinks {host}"
    offers = business_offers(db, user.id, host)

    def _with_link_concepts(body: dict, *, cached: bool) -> dict:
        concepts = backlink_concepts(body, offers)
        return {**body, "concepts": concepts["rows"], "conceptNote": concepts["note"], "cached": cached}

    if stored_only:
        row = _record(db, user.id, "dfs-backlinks", title)
        if row and row.payload:
            return _with_link_concepts(dict(row.payload), cached=True)
        return {"host": host, "state": "none", "cached": True, "concepts": [], "conceptNote": "No referring domains are stored yet. Refresh live backlink data."}
    saved = None if force else cached(db, user.id, "dfs-backlinks", title, WINDOWS["backlinks"])
    if saved and saved.get("qualityVersion") == 2:
        return _with_link_concepts(saved, cached=True)
    quotas.check(db, user, "backlinks")
    _budget(db, user, 3)
    cost = 0.0
    summary_res, c = _call(
        "POST",
        "/backlinks/summary/live",
        [{"target": host, "include_subdomains": True, "backlinks_status_type": "live", "exclude_internal_backlinks": True}],
    )
    cost += c
    s = (summary_res or [{}])[0] or {}
    live_base = {
        "target": host,
        "mode": "one_per_domain",
        "backlinks_status_type": "live",
        "limit": 200,
        "include_subdomains": True,
        "exclude_internal_backlinks": True,
        "order_by": ["domain_from_rank,desc", "rank,desc"],
    }
    try:
        live_res, c = _call(
            "POST",
            "/backlinks/backlinks/live",
            [{**live_base, "filters": [["is_broken", "=", False], "and", ["backlink_spam_score", "<", 60]]}],
        )
    except ResearchError:
        live_res, c = _call("POST", "/backlinks/backlinks/live", [live_base])
    cost += c
    try:
        lost_res, c = _call(
            "POST",
            "/backlinks/backlinks/live",
            [{
                "target": host,
                "mode": "one_per_domain",
                "backlinks_status_type": "lost",
                "limit": 50,
                "include_subdomains": True,
                "exclude_internal_backlinks": True,
                "order_by": ["domain_from_rank,desc", "rank,desc"],
            }],
        )
    except ResearchError:
        lost_res, c = [], 0.0
    cost += c
    live_page = (live_res or [{}])[0] or {}
    lost_page = (lost_res or [{}])[0] or {}
    merged = _collect_backlinks(live_page.get("items") or [])
    merged = _collect_backlinks(lost_page.get("items") or [], prefer=merged)
    links = []
    for row in sorted(merged.values(), key=lambda r: _link_quality(r["_raw"])):
        row.pop("_raw", None)
        links.append(row)
    _spend(db, user, 3, cost)
    quotas.consume(db, user, "backlinks")
    payload = {
        "host": host,
        "summary": {
            "backlinks": s.get("backlinks"),
            "referringDomains": s.get("referring_domains"),
            "referringMainDomains": s.get("referring_main_domains"),
            "nofollowDomains": s.get("referring_domains_nofollow"),
            "brokenBacklinks": s.get("broken_backlinks"),
            "rank": s.get("rank"),
            "rankScale": "DataForSEO domain rank, 0 to 1000 (not Google PageRank)",
            "counts": "Live referring-domain counts from the DataForSEO backlinks index",
        },
        "links": links,
        "linksNote": (
            "Best live link per referring domain first (strongest domain rank, dofollow preferred, spam score under 60). "
            "Lost links are listed separately for review. Spam score is a provider signal requiring review, not a definitive spam judgment. "
            "Anchor text alone does not prove relevance or authority."
        ),
        "linksShown": len(links),
        "referringDomainsListed": live_page.get("total_count"),
        "lostDomainsListed": lost_page.get("total_count"),
        "qualityVersion": 2,
        "market": {"scope": "domain", "countryFilter": False, "note": "Backlink counts are domain-wide, not filtered by the SEO target country."},
        "provider": "dataforseo",
        "dataKind": "backlinks",
        "source": "Searchify SEO Backlinks",
        "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
    }
    concepts = backlink_concepts(payload, offers)
    payload["concepts"] = concepts["rows"]
    payload["conceptNote"] = concepts["note"]
    _save(db, user.id, "dfs-backlinks", title, payload)
    return {**payload, "cached": False}


MODEL_CACHE_VERSION = 4


def _model_for(db: Session, user: User, engine: str) -> dict:
    """{name, reasoning, webSearch} for the cheapest web-searching model Searchify SEO lists for this engine."""
    title = f"Models {engine}"
    saved = cached(db, user.id, "dfs-models", title, WINDOWS["models"])
    if saved and saved.get("version") == MODEL_CACHE_VERSION and (saved.get("model") or {}).get("name"):
        return saved["model"]
    result, _ = _call("GET", f"/ai_optimization/{engine}/llm_responses/models", None, timeout=30)
    try:
        model = llm_requests.pick_model(engine, result)
    except llm_requests.RequestError as exc:
        raise ResearchError(str(exc)) from exc
    _save(db, user.id, "dfs-models", title, {"version": MODEL_CACHE_VERSION, "model": model, "fetchedAt": _now()})
    return model


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _parse_answer(first: dict) -> tuple[str, list[dict]]:
    answer, sources, seen = [], [], set()
    for item in first.get("items") or []:
        for section in item.get("sections") or []:
            if section.get("text"):
                answer.append(section["text"])
            for note in section.get("annotations") or []:
                url = note.get("url")
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"url": url, "title": note.get("title") or ""})
    return "\n".join(answer), sources


def _mentions(body: str, names: set[str]) -> bool:
    """Brand/host mention check — tolerate spaces, hyphens, and punctuation variants."""
    low = (body or "").lower()
    for name in names:
        if not name:
            continue
        compact = re.sub(r"[^a-z0-9]+", "", name)
        if len(compact) >= 3 and compact in re.sub(r"[^a-z0-9]+", "", low):
            return True
        pattern = re.escape(name).replace(r"\ ", r"[\s\-_.]+")
        if re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", low):
            return True
    return False


def visibility(
    db: Session,
    user: User,
    *,
    site: str,
    brand: str,
    prompts: list[dict],
    country: str = "",
    reach: str = "",
    force: bool = False,
) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.", code="site_required")
    if (country or "").strip() or db is None:
        place = locations.resolve(country, reach=reach, allow_worldwide=True)
        market = {"source": "request", "countryIso": place.iso, "version": 0}
    else:
        place, market = _place_for(db, user, site=site, country="", reach=reach, tool="visibility")
    iso, city = place.iso, place.city
    names = {n.lower() for n in [brand, host, host.split(".")[0]] if n and len(n) > 2}
    results, todo = [], []
    for prompt in prompts[:30]:
        text = " ".join(str(prompt.get("text") or "").split())
        engine_label = llm_requests.normalize_engine(prompt.get("engine"))
        if not text:
            continue
        if engine_label in llm_requests.SOON_ENGINES:
            results.append({
                "id": prompt.get("id"),
                "text": text,
                "engine": engine_label,
                "status": "error",
                "code": "engine_unavailable",
                "error": (
                    f"Live checks for {engine_label} are not available yet. "
                    "Choose ChatGPT, Gemini, Perplexity, or Claude (Claude supports country proximity)."
                ),
            })
            continue
        if engine_label not in ENGINES:
            results.append({
                "id": prompt.get("id"),
                "text": text,
                "engine": engine_label,
                "status": "error",
                "code": "unknown_engine",
                "error": f"Unknown answer engine: {prompt.get('engine') or engine_label}.",
            })
            continue
        if len(text) > llm_requests.PROMPT_LIMIT:
            results.append({"id": prompt.get("id"), "text": text, "engine": engine_label, "status": "error",
                            "error": f"Prompt is {len(text)} characters. Shorten it to {llm_requests.PROMPT_LIMIT}."})
            continue
        digest = hashlib.sha1(f"{iso}|{city.lower()}|{text.lower()}".encode()).hexdigest()[:16]
        title = f"Visibility {host} {engine_label} {digest}"[:200]
        saved = None if force else cached(db, user.id, "dfs-visibility", title, WINDOWS["visibility"])
        if saved and saved.get("status", "ok") == "ok":
            reading = visibility_reading(saved, business_offers(db, user.id, host))
            results.append({**saved, **reading, "cached": True})
        else:
            todo.append((text, engine_label, title, prompt.get("id")))
    allowed = quotas.left(db, user, "visibility")
    if allowed is not None and len(todo) > allowed:
        if not allowed and not results:
            quotas.check(db, user, "visibility", len(todo))
        message = str(quotas.QuotaError("visibility", quotas.PLANS[quotas.plan_for(db, user)]["monthly"]["visibility"], quotas.plan_for(db, user)))
        for text, engine_label, _title, prompt_id in todo[allowed:]:
            results.append({"id": prompt_id, "text": text, "engine": engine_label, "status": "error", "code": "quota_exceeded", "error": message})
        todo = todo[:allowed]
    if todo:
        _budget(db, user, len(todo))
    spent, calls = 0.0, 0
    for text, engine_label, title, prompt_id in todo:
        engine = ENGINES[engine_label]
        previous = dict(((_record(db, user.id, "dfs-visibility", title) or FeatureRecord()).payload) or {})
        try:
            model = _model_for(db, user, engine)
            payload, applied = llm_requests.build(engine, prompt=text, model=model, iso=iso, city=city)
            calls += 1
            try:
                result, cost = _call("POST", f"/ai_optimization/{engine}/llm_responses/live", [payload], timeout=130)
            except ResearchError as exc:
                # Provider docs and live accounts disagree on some models. Retry once without location fields.
                msg = str(exc)
                if "web_search_country_iso_code" in msg or "web_search_city" in msg:
                    payload.pop("web_search_country_iso_code", None)
                    payload.pop("web_search_city", None)
                    applied = {**applied, "country": "", "city": "", "note": (applied.get("note") or "") + " Location fields were omitted after the provider rejected them."}
                    result, cost = _call("POST", f"/ai_optimization/{engine}/llm_responses/live", [payload], timeout=130)
                else:
                    raise
            spent += cost
        except (ResearchError, llm_requests.RequestError) as exc:
            log.warning("visibility_failed", extra={"engine": engine, "host": host, "error": str(exc)[:200]})
            failed = {"id": prompt_id, "text": text, "engine": engine_label, "status": "error", "error": str(exc), "failedAt": _now()}
            if previous.get("fetchedAt") and previous.get("status", "ok") == "ok":
                failed.update({k: previous.get(k) for k in ("model", "mention", "citation", "sources", "snippet", "fetchedAt", "location")})
                failed["stale"] = True
            results.append(failed)
            continue
        first = (result or [{}])[0] or {}
        body, sources = _parse_answer(first)
        cited = [s["url"] for s in sources if host_of(s["url"]) == host]
        mentioned = _mentions(body, names) if body else None
        row = {
            "id": prompt_id,
            "text": text,
            "engine": engine_label,
            "status": "ok" if body else "empty",
            "model": first.get("model_name") or model["name"],
            "mention": mentioned,
            "citation": bool(cited),
            "citationUrl": cited[0] if cited else "",
            "sources": sources[:12],
            "snippet": body[:1600],
            "fetchedAt": _now(),
            "country": applied["country"],
            "location": {"requested": place.to_dict(), "applied": applied},
            "market": {"countryIso": place.iso, "language": place.language, "source": market.get("source"), "version": market.get("version"), "scope": "country" if applied.get("country") else "global"},
            "provider": "dataforseo",
            "dataKind": "ai_visibility_live",
            "observed": True,
            "estimate": False,
            "live": True,
        }
        row.update(visibility_reading(row, business_offers(db, user.id, host)))
        if row["status"] == "ok" or not previous.get("fetchedAt"):
            _save(db, user.id, "dfs-visibility", title, row)
        results.append({**row, "cached": False})
    if calls:
        _spend(db, user, calls, spent)
        quotas.consume(db, user, "visibility", calls)
    return {
        "host": host,
        "location": place.to_dict(),
        "market": {"countryIso": place.iso, "language": place.language, "source": market.get("source"), "version": market.get("version")},
        "results": results,
        "provider": "dataforseo",
        "dataKind": "ai_visibility_live",
        "observed": True,
        "estimate": False,
        "live": True,
        "source": "Searchify SEO AI",
        "note": (
            "These rows are live DataForSEO LLM response checks for the listed engines only. "
            "They are not the operator likelihood estimate report."
        ),
    }


# key: (finding name, severity, fix, a title/description draft can fix it)
PAGE_CHECKS = {
    "is_5xx_code": ("Pages return a server error", "Critical", "Ask the host or developer to fix the server error. Google drops pages that keep failing.", False),
    "is_4xx_code": ("Pages return a not-found error", "Critical", "Restore the page or redirect it to the closest live page, then update the links that point to it.", False),
    "no_title": ("Pages have no title", "Critical", "Draft a title for each page and approve it before publishing.", True),
    "duplicate_title": ("Pages share the same title", "Warning", "Give each page a title about its own subject so Google can tell them apart.", True),
    "no_description": ("Pages have no meta description", "Warning", "Draft a description so Google shows your words instead of a random snippet.", True),
    "duplicate_description": ("Pages share the same description", "Warning", "Write a description for each page that matches what that page offers.", True),
    "title_too_long": ("Titles get cut off in Google", "Notice", "Shorten the title so the main promise fits before the cut.", True),
    "title_too_short": ("Titles are too short to compete", "Notice", "Add the service, the place, or the benefit to the title.", True),
    "description_too_long": ("Meta descriptions get cut off in Google", "Notice", "Keep the description under about 160 characters so the full sentence shows.", True),
    "description_too_short": ("Meta descriptions are too short", "Notice", "Write one or two sentences (about 120–160 characters) about what the page offers and the next step.", True),
    "irrelevant_title": ("Titles do not match the page content", "Warning", "Rewrite the title from what the page actually says.", True),
    "irrelevant_description": ("Descriptions do not match the page content", "Notice", "Rewrite the description from what the page actually says.", True),
    "no_h1_tag": ("Pages have no main heading (H1)", "Warning", "Add one clear H1 that names the page subject.", False),
    "duplicate_content": ("Pages repeat the same content", "Warning", "Merge the pages, or make each one about a different service or place.", False),
    "canonical_to_broken": ("Canonical tags point to broken pages", "Critical", "Point the canonical tag to the live version of the page.", False),
    "canonical_to_redirect": ("Canonical tags point to redirects", "Warning", "Point the canonical tag straight to the final URL.", False),
    "recursive_canonical": ("Canonical tags loop", "Warning", "Each canonical should point to one final live page.", False),
    "redirect_chain": ("Links go through redirect chains", "Notice", "Link straight to the final URL so visitors and Google skip the extra hops.", False),
    "is_redirect": ("Internal links point to redirects", "Notice", "Update internal links to the final URL.", False),
    "high_loading_time": ("Pages load slowly", "Warning", "Compress images and remove heavy scripts on these pages.", False),
    "large_page_size": ("Pages are very heavy", "Notice", "Reduce image and script weight on these pages.", False),
    "low_content_rate": ("Pages have very little text", "Notice", "Add useful text about the service, the place, and common questions.", False),
    "is_orphan_page": ("Pages have no internal links pointing to them", "Notice", "Link to these pages from related pages so visitors and Google can find them.", False),
    "no_image_alt": ("Images have no alt text", "Warning", "Write a short description of what each image shows. That is the image SEO for the page.", False),
    "no_image_title": ("Images have no title text", "Notice", "Add a short title on important images. The alt text is the part that matters most.", False),
    "https_to_http_links": ("Secure pages link to insecure pages", "Notice", "Change the links to https.", False),
    "seo_friendly_url": ("URLs are hard to read", "Notice", "Use short words in URLs when you next restructure. Redirect the old URL if you change it.", False),
    "lorem_ipsum": ("Pages still contain placeholder paragraphs", "Warning", "Replace the placeholder text with a real paragraph about this page.", False),
    "low_readability_rate": ("Paragraphs are hard to read", "Warning", "Break long paragraphs into shorter ones and use everyday words.", False),
    "low_character_count": ("Paragraphs are too thin", "Notice", "Add a few sentences that explain the offer, who it is for, and the next step.", False),
    "has_micromarkup_errors": ("The page schema has errors", "Warning", "Approve a title so Searchify can write a clean WebPage schema, or fix the existing schema.", True),
    "has_render_blocking_resources": ("Scripts or styles slow the first view", "Warning", "Ask the site builder to load heavy scripts after the page. Searchify does not edit the theme files.", False),
    "deprecated_html_tags": ("Pages use outdated formatting tags", "Notice", "Replace old formatting tags with normal headings and paragraphs.", False),
    "duplicate_meta_tags": ("Pages repeat the same meta tags", "Warning", "Keep one title and one description. Searchify can draft the single listing.", True),
    "duplicate_title_tag": ("Pages have more than one title tag", "Warning", "Keep one title tag. Searchify can draft the title to keep.", True),
    "no_favicon": ("The site has no favicon", "Notice", "Add a small site icon in the WordPress customizer.", False),
}
SEVERITY_ORDER = {"Critical": 0, "Warning": 1, "Notice": 2}

CHECK_GUIDE = {
    "no_title": ("Meta", ["Open the page.", "Let Searchify draft the title, or type one that names the page in the first words.", "Approve it before it is published."]),
    "no_description": ("Meta", ["Open the page.", "Let Searchify draft the description from the page, or write one sentence about who it is for and the next step.", "Approve it before it is published."]),
    "title_too_long": ("Typography", ["Keep the title between 50 and 60 characters.", "Put the keyword and the page topic first.", "Let Searchify draft it if you would rather not edit by hand."]),
    "title_too_short": ("Typography", ["Add the service or the place so the title is about 50 to 60 characters.", "Let Searchify draft it from the page."]),
    "description_too_long": ("Meta", ["Keep the description between about 120 and 160 characters.", "Put the offer and the next step first so nothing important is cut.", "Let Searchify draft it from the page."]),
    "description_too_short": ("Meta", ["Write about 120 to 160 characters: what the page offers, who it is for, the next step.", "Let Searchify draft it from the page."]),
    "irrelevant_title": ("Meta", ["Read the heading on the page.", "The title should say that same thing.", "Let Searchify draft a title that stays on the page topic."]),
    "no_h1_tag": ("Typography", ["In WordPress, set one heading at the top of the page.", "That heading should name the subject in plain words."]),
    "no_image_alt": ("Images", ["Open the page in WordPress.", "Select each image and fill in the alt text with what the picture shows.", "Update the page. Searchify does not replace the image file."]),
    "no_image_title": ("Images", ["On the important photos, add a short image title.", "The alt text is the part visitors and Google use."]),
    "lorem_ipsum": ("Paragraphs", ["Find the placeholder paragraph.", "Replace it with a real explanation of this page.", "Update the page."]),
    "low_readability_rate": ("Paragraphs", ["Split any paragraph longer than four lines.", "Use the same words you would say to a customer."]),
    "low_character_count": ("Paragraphs", ["Add a short paragraph: what this page offers, who it is for, and what to do next."]),
    "low_content_rate": ("Paragraphs", ["Open the page named in this list.", "Read the word count beside it.", "Add a paragraph about what this page offers, who it is for, and the next step.", "Update the page, then crawl again. Searchify does not write the body copy."]),
    "paragraph_content": ("Paragraphs", ["Open the page named beside the word count.", "Replace a thin or hard-to-read paragraph with a few plain sentences.", "Say what the page offers, who it is for, and what to do next.", "Searchify does not rewrite the paragraph for you."]),
    "heading_order": ("Typography", ["Open the page.", "Keep one H1 that names the subject.", "Put H2 sections under that H1. Do not jump from H1 to H3.", "Update the page. Searchify does not edit the theme headings."]),
    "render_mode": ("Render", ["This crawl reads the first HTML, before JavaScript runs.", "Server-rendered means the paragraph is already in that HTML.", "Client-rendered means the first HTML is thin and scripts are expected to fill the page. Many crawlers still read the thin HTML.", "Ask the site builder to put the main paragraph in the server HTML. Searchify does not change the theme."]),
    "has_micromarkup_errors": ("Schema", ["Approve the title and description.", "On publish, leave Open Graph and JSON-LD turned on so Searchify writes a clean page schema."]),
    "missing_open_graph": ("Social", ["Approve the title and description.", "On publish, leave Open Graph turned on. The social title and description use that same listing."]),
    "missing_json_ld": ("Schema", ["Approve the title and description.", "On publish, leave JSON-LD turned on. Searchify writes a WebPage schema from that listing."]),
    "faq_without_schema": ("FAQ", ["Keep the questions visible on the page as real headings.", "Answer each one in the paragraph under it.", "Approving the page schema does not invent answers that are not written on the page."]),
    "video_without_details": ("Video", ["Give the video page a title that says what the video is about.", "Add one paragraph that describes the video.", "Add the video on the page itself. Searchify does not upload the file."]),
    "missing_share_image": ("Social", ["Choose one clear image that represents the page.", "Set it as the social share image in your SEO plugin.", "Searchify does not create the image."]),
}


def _guide(key: str, fix: str) -> tuple[str, list[str]]:
    area, steps = CHECK_GUIDE.get(key, ("Page", []))
    if not steps:
        steps = [fix, "Do this on the page, then crawl the site again to confirm it is gone."]
    return area, steps


def _social_value(social: dict, *names: str) -> str:
    for name in names:
        value = social.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _listing_gaps(items: list[dict]) -> list[dict]:
    """Open Graph, schema, FAQ, and video gaps read from the pages already crawled."""
    missing_og, missing_schema, missing_image, faq_pages, video_pages = [], [], [], [], []
    for item in items:
        if item.get("resource_type") not in (None, "", "html"):
            continue
        meta = item.get("meta") or {}
        checks = item.get("checks") or {}
        social = meta.get("social_media_tags") if isinstance(meta.get("social_media_tags"), dict) else {}
        title = str(meta.get("title") or "").strip()
        if title and not (_social_value(social, "og:title", "og_title") and _social_value(social, "og:description", "og_description")):
            missing_og.append(item)
        if title and not _social_value(social, "og:image", "og_image"):
            missing_image.append(item)
        if checks.get("has_micromarkup") is False:
            missing_schema.append(item)
        headings = []
        htags = meta.get("htags") if isinstance(meta.get("htags"), dict) else {}
        for level in ("h2", "h3"):
            values = htags.get(level) or []
            if isinstance(values, list):
                headings.extend(str(value) for value in values)
        if sum(1 for heading in headings if heading.strip().endswith("?")) >= 2 and checks.get("has_micromarkup") is not True:
            faq_pages.append(item)
        if re.search(r"\b(video|webinar|watch)\b", title, re.I) and "video" not in " ".join(social.keys()).lower():
            video_pages.append(item)
    specs = [
        ("missing_open_graph", "Pages are missing Open Graph titles for social shares", "Warning",
         "Publish the approved title with Open Graph turned on.", True, missing_og),
        ("missing_json_ld", "Pages have no JSON-LD schema", "Warning",
         "Publish the approved title with JSON-LD turned on.", True, missing_schema),
        ("missing_share_image", "Pages have no social share image", "Notice",
         "Set a share image in the SEO plugin. Searchify does not create the picture.", False, missing_image),
        ("faq_without_schema", "Question headings are not backed by FAQ schema", "Notice",
         "Write the answer under each question on the page. Searchify does not invent FAQ answers.", False, faq_pages),
        ("video_without_details", "Video pages are missing video details for search and social", "Notice",
         "Describe the video in the title and the first paragraph, and keep the video on the page.", False, video_pages),
    ]
    issues = []
    for key, name, severity, fix, meta, pages in specs:
        if not pages:
            continue
        area, steps = _guide(key, fix)
        issues.append({
            "key": key,
            "name": name,
            "severity": severity,
            "fix": fix,
            "meta": meta,
            "area": area,
            "steps": steps,
            "count": len(pages),
            "pages": [_page_snippet(page) for page in pages[:25]],
        })
    return issues


def _audit_target(site: str) -> tuple[str, str]:
    raw = (site or "").strip()
    if not re.match(r"^[a-z]+://", raw, re.I):
        raw = f"https://{raw}"
    netloc = re.sub(r"^[a-z]+://", "", raw, flags=re.I).split("/")[0].split("?")[0].lower()
    return netloc, raw


def _audit_limit(user: User) -> int:
    if user.role == "ROLE_ADMIN":
        return AUDIT_PAGES["scale"]
    plan = (getattr(user, "plan", None) or "starter").lower()
    return AUDIT_PAGES.get(plan, AUDIT_PAGES["starter"])


def _page_snippet(item: dict, *, detail: str = "") -> dict:
    meta = item.get("meta") or {}
    row = {
        "url": item.get("url") or "",
        "status": item.get("status_code"),
        "title": str(meta.get("title") or "")[:200],
        "description": str(meta.get("description") or "")[:300],
    }
    if detail:
        row["detail"] = detail[:400]
    return row


def _word_count(item: dict) -> int | None:
    content = (item.get("meta") or {}).get("content")
    if not isinstance(content, dict):
        return None
    raw = content.get("plain_text_word_count")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0, int(raw))


def _script_count(item: dict) -> int | None:
    raw = (item.get("meta") or {}).get("scripts_count")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0, int(raw))


def _heading_levels(item: dict) -> dict[str, list[str]]:
    htags = (item.get("meta") or {}).get("htags")
    levels: dict[str, list[str]] = {}
    if not isinstance(htags, dict):
        return levels
    for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
        values = htags.get(level) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        texts = [str(value).strip() for value in values if str(value).strip()]
        if texts:
            levels[level] = texts
    return levels


def _heading_problem(levels: dict[str, list[str]]) -> str:
    present = [level for level in ("h1", "h2", "h3", "h4", "h5", "h6") if levels.get(level)]
    if not present:
        return "No headings were found in the first HTML."
    notes = []
    h1 = levels.get("h1") or []
    if not h1:
        notes.append("There is no H1. Use one H1 that names the page, then H2 under it.")
    elif len(h1) > 1:
        notes.append(f"There are {len(h1)} H1 headings. Keep one H1.")
    order = [int(level[1]) for level in present]
    for earlier, later in zip(order, order[1:]):
        if later - earlier > 1:
            notes.append(f"The outline jumps from H{earlier} to H{later}. Add the missing level.")
    if not notes:
        return ""
    sequence = " → ".join(level.upper() for level in present)
    return f"Heading order is {sequence}. " + " ".join(notes)


def _render_sentence(words: int | None, scripts: int | None) -> tuple[str, str]:
    """(mode, sentence). mode is ssr, csr, thin, or unknown."""
    if words is None:
        return "unknown", ""
    if words >= 120:
        return "ssr", f"Server-rendered. The first HTML already has {words} words, so the paragraph is in the response before JavaScript."
    if isinstance(scripts, int) and scripts >= 8:
        return "csr", (
            f"Likely client-rendered. The first HTML has {words} words and {scripts} scripts. "
            "Visitors may see more after JavaScript. This crawl reads the server HTML."
        )
    return "thin", f"The server HTML has {words} words and few scripts, so this is thin copy, not a JavaScript-only page."


def _content_issue(key: str, name: str, severity: str, fix: str, pages: list[dict]) -> dict:
    area, steps = _guide(key, fix)
    return {
        "key": key,
        "name": name,
        "severity": severity,
        "fix": fix,
        "meta": False,
        "area": area,
        "steps": steps,
        "count": len(pages),
        "pages": pages[:25],
    }


def _content_findings(items: list[dict]) -> list[dict]:
    """Page-specific low copy, heading order, paragraphs, and server versus client render."""
    low_pages, heading_pages, paragraph_pages, render_pages = [], [], [], []
    csr = False
    for item in items:
        if item.get("resource_type") not in (None, "", "html"):
            continue
        url = item.get("url") or ""
        if not url:
            continue
        checks = item.get("checks") or {}
        words = _word_count(item)
        scripts = _script_count(item)
        mode, render = _render_sentence(words, scripts)
        low_flag = _check_true(checks, "low_content_rate") or _check_true(checks, "low_character_count")
        if words is not None and words < 120 or low_flag:
            count = f"{words} words" if words is not None else "a low amount of text"
            low_pages.append(_page_snippet(
                item,
                detail=f"This page has {count} in the first HTML. Add a paragraph about the offer, who it is for, and the next step.",
            ))
        problem = _heading_problem(_heading_levels(item))
        unknown_outline = problem.startswith("No headings") and words is None and not low_flag
        if problem and not unknown_outline:
            heading_pages.append(_page_snippet(item, detail=problem))
        paragraph_flag = _check_true(checks, "low_readability_rate") or _check_true(checks, "low_character_count") or _check_true(checks, "lorem_ipsum")
        if paragraph_flag or (words is not None and words < 120):
            if words is not None:
                sentence = f"Readable copy on this page is {words} words."
            else:
                sentence = "The paragraphs on this page were flagged as thin or hard to read."
            if _check_true(checks, "low_readability_rate"):
                sentence += " The sentences are hard to read."
            if _check_true(checks, "lorem_ipsum"):
                sentence += " Placeholder text is still on the page."
            paragraph_pages.append(_page_snippet(item, detail=sentence))
        if render:
            if mode == "csr":
                csr = True
            render_pages.append(_page_snippet(item, detail=render))
    findings = []
    if low_pages:
        severe = any("has 0 words" in (page.get("detail") or "") or re.search(r"has ([0-9]|[1-3][0-9]) words", page.get("detail") or "") for page in low_pages)
        findings.append(_content_issue(
            "low_content_rate",
            "Pages with low content",
            "Warning" if severe else "Notice",
            "Add a real paragraph on each page named here. Searchify does not write the body copy.",
            low_pages,
        ))
    if heading_pages:
        findings.append(_content_issue(
            "heading_order",
            "Heading order is broken",
            "Warning",
            "Use one H1, then H2, and do not skip a level.",
            heading_pages,
        ))
    if paragraph_pages:
        findings.append(_content_issue(
            "paragraph_content",
            "Paragraphs need a clearer page explanation",
            "Notice",
            "Rewrite the thin paragraph on each page named here.",
            paragraph_pages,
        ))
    if render_pages:
        findings.append(_content_issue(
            "render_mode",
            "Server and client render",
            "Warning" if csr else "Notice",
            "Read each page's render note. Text that exists only after JavaScript can be missed.",
            render_pages,
        ))
    return findings


def _check_true(checks: dict, key: str) -> bool:
    value = checks.get(key)
    return value is True or value == 1 or str(value).lower() == "true"


DESC_MAX, DESC_MIN = 160, 70


def _meta_length(item: dict, field: str) -> int:
    meta = item.get("meta") or {}
    raw = meta.get(f"{field}_length")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    return len(str(meta.get(field) or "").strip())


def _length_detail(item: dict, key: str) -> str:
    if key in {"title_too_long", "title_too_short"}:
        return f"Title is {_meta_length(item, 'title')} characters. Aim for about 50–60."
    if key in {"description_too_long", "description_too_short"}:
        return f"Meta description is {_meta_length(item, 'description')} characters. Aim for about 120–160."
    return ""


def _pages_with_check(items: list[dict], key: str) -> list[dict]:
    if key == "description_too_long":
        return [i for i in items if _meta_length(i, "description") > DESC_MAX]
    if key == "description_too_short":
        return [i for i in items if 0 < _meta_length(i, "description") < DESC_MIN]
    hit = [i for i in items if _check_true(i.get("checks") or {}, key)]
    if hit or key not in {"no_title", "no_description"}:
        return hit
    # Some crawls omit checks.no_title unless canonical validation ran — fall back to empty meta.
    if key == "no_title":
        return [i for i in items if not str((i.get("meta") or {}).get("title") or "").strip()]
    if key == "no_description":
        return [i for i in items if not str((i.get("meta") or {}).get("description") or "").strip()]
    return hit


def _fetch_checked_pages(task_id: str, key: str, limit: int = 25) -> list[dict]:
    """Ask Searchify SEO for pages that failed one check — more reliable than scanning a partial page dump."""
    try:
        result, _ = _call(
            "POST",
            "/on_page/pages",
            [{
                "id": task_id,
                "limit": limit,
                "filters": [["resource_type", "=", "html"], "and", [f"checks.{key}", "=", True]],
            }],
            timeout=60,
        )
    except ResearchError:
        return []
    return list(((result or [{}])[0] or {}).get("items") or [])


def _audit_report(task_id: str, summary: dict) -> dict:
    pages_res, _ = _call("POST", "/on_page/pages", [{"id": task_id, "limit": 1000}], timeout=90)
    items = [i for i in (((pages_res or [{}])[0] or {}).get("items") or []) if i.get("resource_type") == "html"]
    broken_res, _ = _call("POST", "/on_page/links", [{"id": task_id, "filters": ["is_broken", "=", True], "limit": 100}], timeout=60)
    broken = [
        {"from": link.get("link_from") or "", "to": link.get("link_to") or "", "status": link.get("page_to_status_code")}
        for link in (((broken_res or [{}])[0] or {}).get("items") or [])
    ]

    metrics = summary.get("page_metrics") or {}
    metric_checks = metrics.get("checks") if isinstance(metrics.get("checks"), dict) else {}
    issues = []
    for key, (name, severity, fix, meta) in PAGE_CHECKS.items():
        if key in {"low_content_rate", "low_character_count"}:
            continue
        hit = _pages_with_check(items, key)
        counted = metric_checks.get(key)
        count_hint = int(counted) if isinstance(counted, (int, float)) and counted else 0
        if not hit and count_hint:
            hit = _fetch_checked_pages(task_id, key)
        if not hit and not count_hint:
            continue
        area, steps = _guide(key, fix)
        pages = [_page_snippet(i, detail=_length_detail(i, key)) for i in hit[:25]]
        # Prefer real page URLs. A summary metric alone must not invent blank URL rows.
        if pages:
            count = max(len(pages), count_hint) if count_hint else len(pages)
        elif count_hint:
            count = count_hint
        else:
            continue
        issues.append({
            "key": key,
            "name": name,
            "severity": severity,
            "fix": fix,
            "meta": meta,
            "area": area,
            "steps": steps,
            "count": count,
            "pages": pages,
            "urlsMissing": bool(count_hint and not pages),
            "countSource": "pages" if pages else "summary_metric",
        })
    issues.extend(_listing_gaps(items))
    issues.extend(_content_findings(items))
    from app.page_weight import weight_issues
    issues.extend(weight_issues(items))
    if broken:
        issues.append({
            "key": "broken_links",
            "name": "Links point to broken pages",
            "severity": "Critical",
            "fix": "Update or remove each broken link. Redirect the dead URL if other sites link to it.",
            "meta": False,
            "area": "Links",
            "steps": ["Open the page that contains the link.", "Point it at a live page, or remove it.", "If other sites link to the dead address, add a redirect."],
            "count": len(broken),
            "pages": [{"url": b["from"], "status": b["status"], "title": f"Links to {b['to']}", "description": ""} for b in broken[:25]],
        })

    domain = summary.get("domain_info") or {}
    checks = domain.get("checks") or {}
    site_checks = [
        ("ssl", "The site has no valid SSL certificate", "Critical", "Install or renew the certificate so every page loads on https."),
        ("sitemap", "No XML sitemap was found", "Warning", "Publish a sitemap and submit it in Search Console."),
        ("robots_txt", "No robots.txt file was found", "Notice", "Add a robots.txt that points to the sitemap."),
        ("test_https_redirect", "http does not redirect to https", "Warning", "Redirect every http address to its https version."),
    ]
    for key, name, severity, fix in site_checks:
        if key in checks and checks.get(key) is False:
            issues.append({"key": key, "name": name, "severity": severity, "fix": fix, "meta": False, "area": "Technical",
                           "steps": [fix, "This is a site setting. Crawl again after it is in place."], "count": 1,
                           "pages": [{"url": f"https://{domain.get('name') or ''}/", "status": None, "title": "", "description": ""}]})

    issues.sort(key=lambda row: (SEVERITY_ORDER.get(row["severity"], 3), -row["count"]))
    status = summary.get("crawl_status") or {}
    raw_score = metrics.get("onpage_score")
    score = round(float(raw_score), 1) if raw_score is not None else None
    critical_count = sum(1 for row in issues if row["severity"] == "Critical")
    return {
        "score": score,
        "seoScore": score,
        "criticalCount": critical_count,
        "pagesCrawled": status.get("pages_crawled") or len(items),
        "maxPages": status.get("max_crawl_pages"),
        "cms": domain.get("cms") or "",
        "server": domain.get("server") or "",
        "brokenLinks": metrics.get("broken_links"),
        "duplicateTitles": metrics.get("duplicate_title"),
        "nonIndexable": metrics.get("non_indexable"),
        "issues": issues,
    }


def _summary(task_id: str) -> dict | None:
    """None while the crawl is still queued."""
    task, _ = _task("GET", f"/on_page/summary/{task_id}", None, timeout=30, ok=(20000, 40601, 40602))
    if int(task.get("status_code") or 0) != 20000:
        return None
    return (task.get("result") or [{}])[0] or {}


def audit(db: Session, user: User, *, site: str, force: bool = False) -> dict:
    """Starts a Searchify SEO site crawl, or reports its progress, or returns the finished report."""
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.", code="site_required")
    title = f"Audit {host}"
    row = _record(db, user.id, "dfs-audit", title)
    saved = dict((row.payload if row else {}) or {})
    now = datetime.utcnow()

    def age(stamp: str | None) -> timedelta:
        try:
            return now - datetime.fromisoformat(stamp or "")
        except ValueError:
            return timedelta.max

    if saved.get("state") == "crawling" and saved.get("taskId") and age(saved.get("startedAt")) < AUDIT_STALE:
        summary = _summary(saved["taskId"])
        crawl = (summary or {}).get("crawl_status") or {}
        if summary and summary.get("crawl_progress") == "finished":
            report = _audit_report(saved["taskId"], summary)
            payload = {
                **saved,
                "state": "ready",
                "report": report,
                "fetchedAt": now.isoformat(timespec="seconds"),
                "provider": "dataforseo",
                "dataKind": "on_page_audit",
                "limitations": (
                    "HTML crawl with JavaScript disabled. Core Web Vitals and real-user performance "
                    "are not measured here — use PageSpeed / field data for that. "
                    "Findings without page URLs are summary counts only."
                ),
            }
            _save(db, user.id, "dfs-audit", title, payload)
            return {**payload, "cached": False}
        payload = {**saved, "crawled": crawl.get("pages_crawled") or 0, "queued": crawl.get("pages_in_queue") or 0}
        return {**payload, "cached": False}

    if saved.get("state") == "ready" and not force and age(saved.get("fetchedAt")) < WINDOWS["audit"]:
        report = saved.get("report") or {}
        # Rebuild once for older crawls that stored a score but dropped critical findings.
        if saved.get("taskId") and report.get("criticalCount") is None:
            summary = _summary(saved["taskId"])
            if summary and summary.get("crawl_progress") == "finished":
                rebuilt = _audit_report(saved["taskId"], summary)
                payload = {**saved, "state": "ready", "report": rebuilt, "fetchedAt": now.isoformat(timespec="seconds")}
                _save(db, user.id, "dfs-audit", title, payload)
                return {**payload, "cached": False}
        return {**saved, "cached": True}

    quotas.check(db, user, "audits")
    _budget(db, user, 1)
    netloc, start = _audit_target(site)
    limit = _audit_limit(user)
    task, cost = _task(
        "POST",
        "/on_page/task_post",
        [{
            "target": netloc,
            "start_url": start,
            "max_crawl_pages": limit,
            # Image, script and stylesheet byte sizes (heavy images / heavy assets findings) and the
            # render-blocking check need the crawler to fetch those resources. With this off, those
            # reports never appeared. It raises the crawl cost per page.
            "load_resources": True,
            "enable_javascript": False,
            "store_raw_html": False,
            "check_spell": False,
        }],
        ok=(20000, 20100),
    )
    task_id = task.get("id")
    if not task_id:
        raise ResearchError("Searchify SEO did not start the site crawl. Try again in a minute.")
    _spend(db, user, 1, cost)
    quotas.consume(db, user, "audits")
    payload = {
        "host": host,
        "state": "crawling",
        "taskId": task_id,
        "maxPages": limit,
        "startedAt": now.isoformat(timespec="seconds"),
        "crawled": 0,
        "queued": 0,
        "report": saved.get("report"),
        "fetchedAt": saved.get("fetchedAt"),
        "provider": "dataforseo",
        "dataKind": "on_page_audit",
        "source": "Searchify SEO On-Page",
        "limitations": (
            "HTML crawl with JavaScript disabled. Core Web Vitals are not measured from this crawl."
        ),
    }
    _save(db, user.id, "dfs-audit", title, payload)
    return {**payload, "cached": False}
