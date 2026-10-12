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
        .limit(8)
        .all()
    )
    visibility = []
    for row in visibility_rows:
        payload = dict(row.payload or {})
        if payload.get("status") == "ok":
            visibility.append(payload)
    return {
        "keywords": latest("dfs-keywords"),
        "backlinks": latest("dfs-backlinks"),
        "visibility": visibility,
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
    hits.sort(key=lambda row: -(row.get("volume") if isinstance(row.get("volume"), (int, float)) else -1))
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


def research_lines(stored: dict | None, url: str, topic: str) -> str:
    data = stored or {}
    keywords = data.get("keywords") or {}
    ranked = keywords_for_page(keywords.get("ranked") or [], url, topic)
    lines = []
    if ranked:
        lines.append("Searchify SEO keywords for this page (volume and position are estimates already stored):")
        for row in ranked:
            volume = row.get("volume") if row.get("volume") is not None else "—"
            position = row.get("position") if row.get("position") is not None else "—"
            lines.append(f"- {row.get('keyword')} · volume {volume} · position {position}")
    else:
        lines.append("Searchify SEO has no stored keyword that matches this page yet. Use the page topic and Search Console.")
    summary = (data.get("backlinks") or {}).get("summary") or {}
    if summary:
        lines.append(
            "Backlinks for the domain: "
            f"referring domains {summary.get('referringDomains') if summary.get('referringDomains') is not None else '—'}, "
            f"domain rank {summary.get('rank') if summary.get('rank') is not None else '—'}."
        )
        anchors = []
        for link in (data.get("backlinks") or {}).get("links") or []:
            if not isinstance(link, dict):
                continue
            if url and _same_page(str(link.get("target") or ""), url) and link.get("anchor"):
                anchors.append(str(link["anchor"])[:80])
            if len(anchors) >= 4:
                break
        if anchors:
            lines.append("Anchors already pointing at this page: " + "; ".join(anchors))
    else:
        lines.append("No stored backlink summary yet.")
    checks = data.get("visibility") or []
    if checks:
        lines.append("AI visibility checks already stored:")
        for row in checks[:4]:
            mentioned = "mentioned" if row.get("mention") else "not mentioned"
            engine = row.get("engine") or "an answer engine"
            snippet = " ".join(str(row.get("snippet") or "").split())[:140]
            lines.append(f"- {engine}: brand {mentioned}. {snippet}")
    else:
        lines.append("No stored AI visibility check yet.")
    return "\n".join(lines)


def keyword_candidates(stored: dict | None, url: str, topic: str) -> list[str]:
    ranked = keywords_for_page((stored or {}).get("keywords", {}).get("ranked") or [], url, topic)
    words = []
    for row in ranked:
        text = str(row.get("keyword") or "").strip()
        if text and _content_tokens(text):
            words.append(text)
    return words
