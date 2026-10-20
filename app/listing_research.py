"""Stored Searchify SEO research for one page. Reads what is already saved. Does not call the provider."""

from __future__ import annotations

from urllib.parse import urlparse

from app.meta_writer import _content_tokens, _keyword_fit
from app.site_scan import page_key


def load_stored(db, user_id: int) -> dict:
    from app.models import FeatureRecord

    def latest(kind: str) -> dict:
        row = (
            db.query(FeatureRecord)
            .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
            .order_by(FeatureRecord.id.desc())
            .first()
        )
        return dict(row.payload or {}) if row else {}

    visibility_rows = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "dfs-visibility")
        .order_by(FeatureRecord.id.desc())
        .limit(12)
        .all()
    )
    visibility = []
    for row in visibility_rows:
        payload = dict(row.payload or {})
        if payload.get("status") == "ok":
            visibility.append(payload)
    audit = latest("site-audit") or latest("dfs-audit")
    return {
        "keywords": latest("dfs-keywords"),
        "backlinks": latest("dfs-backlinks"),
        "visibility": visibility,
        "audit": audit,
    }


def _same_page(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if page_key(left) == page_key(right):
        return True
    a, b = urlparse(left), urlparse(right)
    return (a.path.rstrip("/") or "/") == (b.path.rstrip("/") or "/") and bool(a.path)


def ranked_on_url(ranked: list, url: str) -> list[dict]:
    """Keywords Searchify SEO already associates with this exact URL."""
    hits = [
        row for row in ranked or []
        if isinstance(row, dict) and row.get("keyword") and _same_page(str(row.get("url") or ""), url)
    ]
    hits.sort(key=lambda row: (-(row.get("volume") if isinstance(row.get("volume"), (int, float)) else -1), (row.get("position") if isinstance(row.get("position"), (int, float)) else 10**6)))
    return hits[:8]


def keywords_for_page(ranked: list, url: str, topic: str) -> list[dict]:
    """Keywords Searchify SEO already ranks for this URL, then keywords that share the page topic."""
    on_page = []
    topical = []
    for row in ranked or []:
        if not isinstance(row, dict) or not row.get("keyword"):
            continue
        if _same_page(str(row.get("url") or ""), url):
            on_page.append(row)
        elif _keyword_fit(str(row.get("keyword")), topic, topic) >= 0.5:
            topical.append(row)
    if on_page:
        return ranked_on_url(ranked, url)
    topical.sort(key=lambda row: -(row.get("volume") if isinstance(row.get("volume"), (int, float)) else -1))
    return topical[:5]


def idea_keywords(stored: dict | None, topic: str, offers: list[str] | None = None, limit: int = 8) -> list[dict]:
    """Stored keyword ideas that still fit this page topic or the business offers."""
    ideas = ((stored or {}).get("keywords") or {}).get("ideas") or []
    offer_blob = " ".join(str(item) for item in (offers or []) if str(item).strip())
    rows = []
    seen: set[str] = set()
    for row in ideas:
        if not isinstance(row, dict):
            continue
        keyword = " ".join(str(row.get("keyword") or "").split())
        key = keyword.lower()
        if not key or key in seen:
            continue
        fit = max(_keyword_fit(keyword, topic, topic), _keyword_fit(keyword, offer_blob, offer_blob) if offer_blob else 0.0)
        if fit < 0.34 and not (_content_tokens(keyword) & (_content_tokens(topic) | _content_tokens(offer_blob))):
            continue
        seen.add(key)
        rows.append({**row, "keyword": keyword, "_fit": fit})
    rows.sort(key=lambda row: (-(row.get("volume") if isinstance(row.get("volume"), (int, float)) else -1), -row.get("_fit", 0)))
    return rows[:limit]


def page_anchors(stored: dict | None, url: str, limit: int = 4) -> list[str]:
    anchors = []
    for link in ((stored or {}).get("backlinks") or {}).get("links") or []:
        if not isinstance(link, dict):
            continue
        if url and _same_page(str(link.get("target") or ""), url) and link.get("anchor"):
            text = " ".join(str(link["anchor"]).split())[:80]
            if text and text.lower() not in {item.lower() for item in anchors}:
                anchors.append(text)
        if len(anchors) >= limit:
            break
    return anchors


def visibility_gap(stored: dict | None) -> str:
    checks = (stored or {}).get("visibility") or []
    if not checks:
        return ""
    named = [row for row in checks if row.get("mention")]
    missed = [row for row in checks if row.get("mention") is False]
    if missed and not named:
        engines = ", ".join(sorted({str(row.get("engine") or "answer engine") for row in missed[:4]}))
        return f"AI visibility gap: the brand was not named in stored checks on {engines}. The listing should state the offer clearly so answer engines can cite this page."
    if named:
        return f"AI visibility: named in {len(named)} of {len(checks)} stored checks. Keep the offer wording clear."
    return ""


def best_keyword_from_research(
    stored: dict | None,
    *,
    url: str,
    topic: str,
    offers: list[str] | None = None,
    queries: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Best keyword for this page from stored Searchify SEO rows, then Search Console queries."""
    keywords = (stored or {}).get("keywords") or {}
    ranked = keywords_for_page(keywords.get("ranked") or [], url, topic)
    ideas = idea_keywords(stored, topic, offers)
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()

    def add(term: str, score: float) -> None:
        text = " ".join(str(term or "").split())[:80]
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        scored.append((score, text))

    for index, row in enumerate(ranked):
        volume = row.get("volume") if isinstance(row.get("volume"), (int, float)) else 0
        position = row.get("position") if isinstance(row.get("position"), (int, float)) else 50
        on_page = 3.0 if _same_page(str(row.get("url") or ""), url) else 1.0
        add(row.get("keyword"), on_page + min(volume or 0, 5000) / 1000 + max(0, 20 - float(position)) / 10 - index * 0.05)
    for index, row in enumerate(ideas):
        volume = row.get("volume") if isinstance(row.get("volume"), (int, float)) else 0
        add(row.get("keyword"), 1.2 + min(volume or 0, 5000) / 1200 + row.get("_fit", 0) - index * 0.04)
    for index, query in enumerate(queries or []):
        add(query, 0.8 - index * 0.05)
    for offer in offers or []:
        add(offer, 0.6)
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return "", []
    return scored[0][1], [text for _score, text in scored[:6]]


def research_lines(stored: dict | None, url: str, topic: str, offers: list[str] | None = None) -> str:
    data = stored or {}
    keywords = data.get("keywords") or {}
    ranked = keywords_for_page(keywords.get("ranked") or [], url, topic)
    ideas = idea_keywords(data, topic, offers)
    lines = []
    if ranked:
        lines.append("Searchify SEO keywords for this page (volume and position are estimates already stored):")
        for row in ranked:
            volume = row.get("volume") if row.get("volume") is not None else "—"
            position = row.get("position") if row.get("position") is not None else "—"
            lines.append(f"- {row.get('keyword')} · volume {volume} · position {position}")
    else:
        lines.append("Searchify SEO has no stored ranked keyword that matches this page yet.")
    if ideas:
        lines.append("Stored keyword ideas that still fit this page (use when the page does not already rank):")
        for row in ideas[:5]:
            volume = row.get("volume") if row.get("volume") is not None else "—"
            lines.append(f"- {row.get('keyword')} · volume {volume}")
    summary = (data.get("backlinks") or {}).get("summary") or {}
    if summary:
        lines.append(
            "Backlinks for the domain: "
            f"referring domains {summary.get('referringDomains') if summary.get('referringDomains') is not None else '—'}, "
            f"domain rank {summary.get('rank') if summary.get('rank') is not None else '—'}."
        )
        anchors = page_anchors(data, url)
        if anchors:
            lines.append("Anchors already pointing at this page: " + "; ".join(anchors))
    else:
        lines.append("No stored backlink summary yet.")
    gap = visibility_gap(data)
    checks = data.get("visibility") or []
    if checks:
        lines.append("AI visibility checks already stored:")
        for row in checks[:4]:
            mentioned = "mentioned" if row.get("mention") else "not mentioned"
            engine = row.get("engine") or "an answer engine"
            snippet = " ".join(str(row.get("snippet") or row.get("text") or "").split())[:140]
            lines.append(f"- {engine}: brand {mentioned}. {snippet}")
        if gap:
            lines.append(gap)
    else:
        lines.append("No stored AI visibility check yet.")
    audit = data.get("audit") or {}
    findings = audit.get("findings") or audit.get("issues") or []
    if isinstance(findings, list) and findings:
        lines.append("Stored site audit notes that can shape the listing (do not invent severity):")
        for item in findings[:5]:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title") or item.get("code") or "Finding"
                detail = item.get("evidence") or item.get("detail") or item.get("fix") or ""
                lines.append(f"- {name}: {' '.join(str(detail).split())[:120]}")
            elif item:
                lines.append(f"- {item}")
    return "\n".join(lines)


def keyword_candidates(stored: dict | None, url: str, topic: str, offers: list[str] | None = None) -> list[str]:
    best, alternatives = best_keyword_from_research(stored, url=url, topic=topic, offers=offers)
    words = []
    for text in [best, *alternatives]:
        if text and _content_tokens(text) and text.lower() not in {item.lower() for item in words}:
            words.append(text)
    return words
