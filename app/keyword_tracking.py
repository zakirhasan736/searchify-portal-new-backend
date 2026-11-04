"""Tracked keywords, current keywords, and business-fit keyword suggestions.

- Tracked keywords live in the database per website (never in the browser).
- Current keywords: DataForSEO ranked keywords first, Search Console queries second.
- Suggestions: DataForSEO keyword data, kept only when the AI read of the website says the search
  belongs to what this business actually sells. Nothing is invented or preset by Searchify.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.concept_reads import match_keyword
from app.models import FeatureRecord, SiteChange, TrackedKeyword
from app.site_scope import host_key, payload_host

log = logging.getLogger("searchify.keywords")

MAX_KEYWORD = 120
# Searches sent to the AI business-fit check per refresh.
POOL = 240
QUESTION = re.compile(r"^(how|what|why|when|where|who|which|can|does|do|is|are|should|will|could|would)\b", re.I)


def is_question(keyword: str) -> bool:
    text = (keyword or "").strip()
    return bool(QUESTION.match(text)) or text.endswith("?")


def clean_keyword(value: str) -> str:
    return " ".join(str(value or "").lower().split())[:MAX_KEYWORD]


# ---------------------------------------------------------------- tracked keywords


def tracked_rows(db: Session, user_id: int, host: str) -> list[TrackedKeyword]:
    if db is None or not host:
        return []
    return (
        db.query(TrackedKeyword)
        .filter(TrackedKeyword.customer_id == user_id, TrackedKeyword.host == host)
        .order_by(TrackedKeyword.created_at.desc(), TrackedKeyword.id.desc())
        .all()
    )


def tracked_terms(db: Session, user_id: int, host: str) -> list[str]:
    return [row.keyword for row in tracked_rows(db, user_id, host)]


def track(
    db: Session,
    user_id: int,
    host: str,
    keyword: str,
    *,
    source: str = "manual",
    concept: str = "",
    page_url: str = "",
    limit: int | None = None,
) -> tuple[TrackedKeyword, bool]:
    """Add one keyword. Returns (row, created). Raises ValueError when empty or over the plan limit."""
    term = clean_keyword(keyword)
    if not term:
        raise ValueError("Enter a keyword to track.")
    host = host_key(host)
    existing = (
        db.query(TrackedKeyword)
        .filter(TrackedKeyword.customer_id == user_id, TrackedKeyword.host == host, TrackedKeyword.keyword == term)
        .first()
    )
    if existing:
        if page_url and not existing.page_url:
            existing.page_url = page_url
        if concept and not existing.concept:
            existing.concept = concept[:200]
        db.commit()
        return existing, False
    if limit is not None:
        count = db.query(TrackedKeyword).filter(TrackedKeyword.customer_id == user_id, TrackedKeyword.host == host).count()
        if count >= limit:
            raise ValueError(f"Your plan tracks up to {limit} keywords for this website. Remove one to add another.")
    row = TrackedKeyword(
        customer_id=user_id,
        host=host,
        keyword=term,
        source=(source or "manual")[:40],
        concept=(concept or "")[:200],
        page_url=page_url or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, True


def untrack(db: Session, user_id: int, host: str, keyword: str) -> bool:
    term = clean_keyword(keyword)
    row = (
        db.query(TrackedKeyword)
        .filter(TrackedKeyword.customer_id == user_id, TrackedKeyword.host == host_key(host), TrackedKeyword.keyword == term)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def tracked_payload(db: Session, user_id: int, host: str, metrics: list[dict] | None = None) -> list[dict]:
    """Tracked keywords with live Google ranks (desktop + mobile history), DataForSEO keyword numbers,
    Search Console clicks, and the state of the page update they started."""
    from app.rank_tracking import rank_history

    by_term: dict[str, dict] = {}
    for item in metrics or []:
        term = clean_keyword(item.get("keyword"))
        if not term:
            continue
        held = by_term.setdefault(term, {})
        for key, value in item.items():
            if value not in (None, "", []) and held.get(key) in (None, "", []):
                held[key] = value
    live = rank_history(db, user_id, host)
    rows = tracked_rows(db, user_id, host)
    change_ids = [row.change_id for row in rows if row.change_id]
    changes = {}
    if change_ids:
        changes = {c.id: c for c in db.query(SiteChange).filter(SiteChange.id.in_(change_ids), SiteChange.customer_id == user_id).all()}
    out = []
    for row in rows:
        data = by_term.get(row.keyword) or {}
        checks = live.get(row.keyword) or {}
        desk = checks.get("desktop") or {}
        change = changes.get(row.change_id) if row.change_id else None
        proposed = dict(change.proposed or {}) if change else {}
        has_live = bool(desk.get("history"))
        out.append({
            **{k: v for k, v in data.items() if k not in {"source", "position", "previous"}},
            "keyword": row.keyword,
            # Live Google check first; DataForSEO's ranked-keywords estimate only until the first check.
            "position": (desk.get("latest") or {}).get("position") if has_live else data.get("position"),
            "previous": desk.get("previous") if has_live else data.get("previous"),
            "rankSource": "live" if has_live else ("estimate" if data.get("position") is not None else "none"),
            "live": checks,
            "trackedSource": row.source,
            "concept": row.concept or data.get("concept") or "",
            "page": row.page_url or (desk.get("latest") or {}).get("url") or data.get("url") or "",
            "trackedAt": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
            "measuring": not has_live and data.get("position") is None,
            "source": "Live Google check (DataForSEO SERP)" if has_live else (data.get("source") or "Waiting for the first live Google check"),
            "change": {
                "id": change.id,
                "status": change.status,
                "title": proposed.get("title") or "",
                "metaDescription": proposed.get("metaDescription") or "",
                "beforeTitle": proposed.get("beforeTitle") or "",
                "beforeDescription": proposed.get("beforeDescription") or proposed.get("beforeMetaDescription") or "",
                "targetUrl": change.target_url or "",
                "appliedAt": change.applied_at.isoformat(timespec="seconds") if change.applied_at else None,
                "updatedAt": change.updated_at.isoformat(timespec="seconds") if change.updated_at else None,
            } if change else None,
        })
    return out


# ---------------------------------------------------------------- current keywords


def _gsc_queries(db: Session, user_id: int, host: str) -> tuple[list[list], str]:
    if db is None:
        return [], ""
    rows = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "organic-search")
        .order_by(FeatureRecord.id.desc())
        .limit(24)
        .all()
    )
    for rec in rows:
        payload = rec.payload or {}
        if payload_host(payload) == host:
            return [r for r in payload.get("rows") or [] if isinstance(r, (list, tuple)) and r and str(r[0]).strip()], str(payload.get("syncedAt") or "")
    return [], ""


def _num(value):
    try:
        text = str(value).replace("%", "").replace(",", "").strip()
        return float(text) if text not in {"", "—", "-"} else None
    except (TypeError, ValueError):
        return None


def current_keywords(db: Session, user_id: int, host: str, ranked: list[dict]) -> dict:
    """Searches this website shows up for today. DataForSEO ranked keywords lead; Search Console
    adds real clicks and impressions, and its own queries fill in what DataForSEO has not seen."""
    gsc_rows, synced = _gsc_queries(db, user_id, host)
    gsc = {}
    for row in gsc_rows:
        term = clean_keyword(row[0])
        gsc[term] = {
            "clicks": _num(row[1]) if len(row) > 1 else None,
            "impressions": _num(row[2]) if len(row) > 2 else None,
            "ctr": str(row[3]) if len(row) > 3 else "",
            "gscPosition": _num(row[4]) if len(row) > 4 else None,
        }
    out, seen = [], set()
    for row in ranked or []:
        term = clean_keyword(row.get("keyword"))
        if not term or term in seen:
            continue
        seen.add(term)
        extra = gsc.get(term) or {}
        out.append({**row, **extra, "keyword": term, "source": "DataForSEO", "alsoInSearchConsole": bool(extra)})
    for term, extra in gsc.items():
        if term in seen:
            continue
        seen.add(term)
        pos = extra.get("gscPosition")
        out.append({
            "keyword": term,
            "position": round(pos) if isinstance(pos, float) else None,
            "volume": None,
            "difficulty": None,
            **extra,
            "source": "Search Console",
            "alsoInSearchConsole": True,
        })
    return {"rows": out, "searchConsoleSyncedAt": synced, "searchConsoleConnected": bool(gsc_rows)}


# ---------------------------------------------------------------- business-fit judging

JUDGE = (
    "You decide which Google searches belong to one business, using only the website read below. "
    "A search belongs when a person typing it could become a customer for something this business sells "
    "(its category or one of its offers), including synonyms, product sizes/models and buying questions. "
    "It does not belong when it is about another trade, a competitor brand, jobs, free/DIY content unrelated "
    "to buying, or a place the business does not serve.\n"
    "Return JSON only: {\"rows\":[{\"k\":<index>,\"fit\":\"core|related|off\",\"concept\":\"which offer or the category\","
    "\"page\":\"the URL from the page list that should rank for it, or empty if no page covers it\"}]}. "
    "core = directly what they sell. related = a buying/research search one step away. "
    "Judge every index you are given."
)


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def judge_fit(concept: dict, places: list[str], candidates: list[dict]) -> dict[str, dict] | None:
    """AI decides which candidates fit the business. None when the AI could not be used
    (the caller then falls back to word matching against the offers)."""
    from app.openai_client import chat_text, openai_configured

    if not candidates or not openai_configured():
        return None
    pages = [p for p in concept.get("pages") or [] if p.get("role") not in {"legal", "contact"}][:40]
    business = (
        f"Business: {concept.get('summary') or '-'}\n"
        f"Category: {concept.get('category') or '-'}\n"
        f"Offers: {'; '.join(concept.get('offers') or []) or '-'}\n"
        f"Places served: {'; '.join(places) or 'not stated'}\n"
        "Pages:\n" + "\n".join(f"- {p['url']} ({p.get('role') or 'page'}): {p.get('target') or ''}" for p in pages)
    )
    listing = "\n".join(
        f"{i}. {row['keyword']} (volume {row.get('volume') if row.get('volume') is not None else 'n/a'})"
        for i, row in enumerate(candidates)
    )
    try:
        text, model, _role = chat_text(
            system=JUDGE,
            user=f"{business}\n\nSearches to judge:\n{listing}",
            kind="analyst",
            writing_type="Keyword business fit",
            force_astra=True,
            max_tokens=8000,
        )
    except RuntimeError as exc:
        log.warning("keyword_fit_judge_failed", extra={"error": str(exc)[:200]})
        return None
    data = _parse_json(text)
    page_urls = {p["url"] for p in pages}
    out: dict[str, dict] = {}
    for item in data.get("rows") or []:
        try:
            index = int(item.get("k"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(candidates):
            continue
        page = str(item.get("page") or "").strip()
        out[clean_keyword(candidates[index]["keyword"])] = {
            "fit": str(item.get("fit") or "off").lower(),
            "concept": str(item.get("concept") or "")[:200],
            "page": page if page in page_urls else "",
            "model": model,
        }
    return out if out else None


def _score(row: dict) -> float:
    volume = row.get("volume") if isinstance(row.get("volume"), (int, float)) else 0
    difficulty = row.get("difficulty") if isinstance(row.get("difficulty"), (int, float)) else 50
    weight = 1.0 if row.get("fit") == "core" else 0.6
    return weight * (volume or 0) / (10 + difficulty)


def business_suggestions(concept: dict, places: list[str], candidates: list[dict], *, known: set[str]) -> dict:
    """Keep only candidates that fit the business. Each kept row says which offer it belongs to and
    which page of the site should rank for it."""
    # One row per search. A competitor-gap hit keeps the rival's details even if another source
    # found the same search first.
    merged: dict[str, dict] = {}
    for row in candidates:
        term = clean_keyword(row.get("keyword"))
        if not term or term in known:
            continue
        held = merged.get(term)
        if held is None:
            merged[term] = {**row, "keyword": term, "kind": row.get("kind") or "site"}
        elif row.get("kind") == "gap" and not held.get("gapDomain"):
            held.update({k: row.get(k) for k in ("gapDomain", "gapPosition", "gapUrl")})
            held.setdefault("alsoFrom", []).append("gap")
    # Take the strongest searches from every source in turn so no source crowds out the others.
    by_kind: dict[str, list[dict]] = {}
    for row in merged.values():
        by_kind.setdefault(row["kind"], []).append(row)
    for rows in by_kind.values():
        rows.sort(key=lambda r: -(r.get("volume") or 0))
    pool = []
    while len(pool) < POOL and any(by_kind.values()):
        for kind in list(by_kind):
            if by_kind[kind]:
                pool.append(by_kind[kind].pop(0))
    verdicts = judge_fit(concept, places, pool)
    kept = []
    for row in pool:
        words = match_keyword(row["keyword"], concept)
        if verdicts is not None:
            verdict = verdicts.get(row["keyword"])
            if not verdict or verdict["fit"] not in {"core", "related"}:
                continue
            kept.append({
                **row,
                "fit": verdict["fit"],
                "concept": verdict["concept"] or words["concept"],
                "page": verdict["page"] or words["page"],
                "pageTarget": words["pageTarget"],
                "judgedBy": verdict["model"],
            })
        elif words["fit"] >= 0.5:
            kept.append({
                **row,
                "fit": "core" if words["fit"] >= 0.75 else "related",
                "concept": words["concept"],
                "page": words["page"],
                "pageTarget": words["pageTarget"],
                "judgedBy": "offer word match",
            })
    kept.sort(key=_score, reverse=True)
    return {
        "rows": kept,
        "judged": "ai" if verdicts is not None else "words",
        "checked": len(pool),
        "at": datetime.utcnow().isoformat(timespec="seconds"),
    }
