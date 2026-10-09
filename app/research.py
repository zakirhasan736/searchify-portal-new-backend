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
from app.models import FeatureRecord, User

log = logging.getLogger("searchify.research")

BASE = "https://api.dataforseo.com/v3"
DAILY_CALLS = {"starter": 40, "growth": 150, "agency": 500, "scale": 800}
WINDOWS = {"keywords": timedelta(hours=24), "backlinks": timedelta(hours=24), "visibility": timedelta(days=7), "models": timedelta(days=7), "audit": timedelta(days=7)}
AUDIT_PAGES = {"starter": 100, "growth": 300, "agency": 1000, "scale": 1000}
AUDIT_STALE = timedelta(hours=3)
ENGINES = llm_requests.ENGINES
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


def _idea_seeds(clean: list[str], ranked: list[dict], limit: int = 3) -> list[str]:
    """Prefer tracked terms, then strongest ranking terms, for suggestion seeds."""
    seeds: list[str] = []
    for term in clean:
        if term and term not in seeds:
            seeds.append(term)
        if len(seeds) >= limit:
            return seeds
    for row in sorted(ranked, key=_rank_key):
        kw = (row.get("keyword") or "").strip()
        if kw and kw.lower() not in {s.lower() for s in seeds}:
            seeds.append(kw)
        if len(seeds) >= limit:
            break
    return seeds


def keywords(db: Session, user: User, *, site: str, terms: list[str], country: str = "", reach: str = "", force: bool = False) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.", code="site_required")
    place, market = _place_for(db, user, site=site, country=country, reach=reach, tool="keywords")
    location, language = place.country, place.language or market.get("language") or "en"
    clean = [t.strip()[:80] for t in dict.fromkeys(t.strip().lower() for t in terms or [] if t and t.strip())][:50]
    term_limit = quotas.tracked_keyword_limit(db, user)
    dropped = len(clean) - term_limit if term_limit is not None and len(clean) > term_limit else 0
    if dropped:
        clean = clean[:term_limit]
    title = f"Keywords {host} {place.iso or location} {language}"
    saved = None if force else cached(db, user.id, "dfs-keywords", title, WINDOWS["keywords"])
    if saved and set(clean) <= set(saved.get("terms") or []) and saved.get("qualityVersion") == 2:
        return {**saved, "cached": True, "termsDropped": dropped}
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

    ideas = []
    seeds = _idea_seeds(clean, ranked, limit=3)
    known = {t.lower() for t in clean} | set(by_term)
    idea_calls = 0
    for seed in seeds:
        ideas_res, c = _call(
            "POST",
            "/dataforseo_labs/google/keyword_suggestions/live",
            [{"keyword": seed, "location_name": location, "language_code": language, "limit": 40,
              "order_by": ["keyword_info.search_volume,desc"]}],
        )
        cost += c
        idea_calls += 1
        for item in ((ideas_res or [{}])[0] or {}).get("items") or []:
            row = _kw_row(item)
            key = (row.get("keyword") or "").lower()
            if not key or key in known:
                continue
            # Prefer ideas with real demand; keep null-volume ideas only as filler.
            known.add(key)
            ideas.append({**row, "source": f"Searchify SEO keyword suggestions · seed “{seed}”", "seed": seed})
    ideas.sort(key=lambda r: (-(r["volume"] if isinstance(r.get("volume"), (int, float)) else -1), r.get("keyword") or ""))
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
        "qualityVersion": 2,
        "source": "Searchify SEO",
        "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
    }
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
    if stored_only:
        row = _record(db, user.id, "dfs-backlinks", title)
        return {**dict(row.payload or {}), "cached": True} if row and row.payload else {"host": host, "state": "none", "cached": True}
    saved = None if force else cached(db, user.id, "dfs-backlinks", title, WINDOWS["backlinks"])
    if saved and saved.get("qualityVersion") == 2:
        return {**saved, "cached": True}
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
            "rankScale": "Searchify SEO domain rank, 0 to 1000",
            "counts": "Live referring domains from Searchify SEO backlinks summary",
        },
        "links": links,
        "linksNote": (
            "Best live link per referring domain first (strongest domain rank, dofollow preferred, spam score under 60). "
            "Lost links are listed separately for review."
        ),
        "linksShown": len(links),
        "referringDomainsListed": live_page.get("total_count"),
        "lostDomainsListed": lost_page.get("total_count"),
        "qualityVersion": 2,
        "market": {"scope": "domain", "countryFilter": False, "note": "Backlink counts are domain-wide, not filtered by the SEO target country."},
        "source": "Searchify SEO Backlinks",
        "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
    }
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
            results.append({**saved, "cached": True})
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
        row = {
            "id": prompt_id,
            "text": text,
            "engine": engine_label,
            "status": "ok" if body else "empty",
            "model": first.get("model_name") or model["name"],
            "mention": _mentions(body, names) if body else None,
            "citation": cited[0] if cited else "",
            "sources": sources[:12],
            "snippet": body[:1600],
            "fetchedAt": _now(),
            "country": applied["country"],
            "location": {"requested": place.to_dict(), "applied": applied},
            "market": {"countryIso": place.iso, "language": place.language, "source": market.get("source"), "version": market.get("version"), "scope": "country" if applied.get("country") else "global"},
        }
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
        "source": "Searchify SEO AI",
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
    "no_image_alt": ("Images have no alt text", "Notice", "Describe each image in a few words for screen readers and image search.", False),
    "https_to_http_links": ("Secure pages link to insecure pages", "Notice", "Change the links to https.", False),
    "seo_friendly_url": ("URLs are hard to read", "Notice", "Use short words in URLs when you next restructure. Redirect the old URL if you change it.", False),
}
SEVERITY_ORDER = {"Critical": 0, "Warning": 1, "Notice": 2}


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


def _page_snippet(item: dict) -> dict:
    meta = item.get("meta") or {}
    return {
        "url": item.get("url") or "",
        "status": item.get("status_code"),
        "title": str(meta.get("title") or "")[:200],
        "description": str(meta.get("description") or "")[:300],
    }


def _check_true(checks: dict, key: str) -> bool:
    value = checks.get(key)
    return value is True or value == 1 or str(value).lower() == "true"


def _pages_with_check(items: list[dict], key: str) -> list[dict]:
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
        hit = _pages_with_check(items, key)
        counted = metric_checks.get(key)
        count_hint = int(counted) if isinstance(counted, (int, float)) and counted else 0
        if not hit and count_hint:
            hit = _fetch_checked_pages(task_id, key)
        if not hit and not count_hint:
            continue
        issues.append({
            "key": key,
            "name": name,
            "severity": severity,
            "fix": fix,
            "meta": meta,
            "count": max(len(hit), count_hint) or len(hit),
            "pages": [_page_snippet(i) for i in hit[:25]] or (
                [{"url": "", "status": None, "title": "", "description": ""}] if count_hint else []
            ),
        })
    if broken:
        issues.append({
            "key": "broken_links",
            "name": "Links point to broken pages",
            "severity": "Critical",
            "fix": "Update or remove each broken link. Redirect the dead URL if other sites link to it.",
            "meta": False,
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
            issues.append({"key": key, "name": name, "severity": severity, "fix": fix, "meta": False, "count": 1,
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
            payload = {**saved, "state": "ready", "report": report, "fetchedAt": now.isoformat(timespec="seconds")}
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
            "load_resources": False,
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
        "source": "Searchify SEO On-Page",
    }
    _save(db, user.id, "dfs-audit", title, payload)
    return {**payload, "cached": False}
