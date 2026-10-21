"""Stored Searchify SEO research for one page. Reads what is already saved. Does not call the provider."""

from __future__ import annotations

from urllib.parse import urlparse

from app.meta_writer import _content_tokens, _keyword_fit
from app.site_scan import page_key


def load_stored(db, user_id: int, site: str = "") -> dict:
    """Load the latest stored research for this customer, scoped to ``site`` when given.

    Without a site host, returns the newest row of each kind (legacy). With a site,
    only rows whose payload ``host`` matches are used — preventing cross-site bleed
    into meta prompts and keyword selection.
    """
    from app.models import FeatureRecord
    from app.research import host_of

    want = host_of(site) if site else ""

    def latest(kind: str) -> dict:
        rows = (
            db.query(FeatureRecord)
            .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
            .order_by(FeatureRecord.id.desc())
            .limit(24)
            .all()
        )
        for row in rows:
            payload = dict(row.payload or {})
            if want and host_of(str(payload.get("host") or "")) != want:
                continue
            return payload
        if want:
            return {}
        return dict(rows[0].payload or {}) if rows else {}

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
        if want and host_of(str(payload.get("host") or "")) != want:
            continue
        if payload.get("status") == "ok":
            visibility.append(payload)

    # On-page HTML findings come from DataForSEO crawl (dfs-audit), not PageSpeed site-audit.
    dfs_audit = latest("dfs-audit")
    return {
        "keywords": latest("dfs-keywords"),
        "backlinks": latest("dfs-backlinks"),
        "visibility": visibility,
        "audit": dfs_audit,
        "host": want,
    }


def _same_page(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if page_key(left) == page_key(right):
        return True
    a, b = urlparse(left), urlparse(right)
    return (a.path.rstrip("/") or "/") == (b.path.rstrip("/") or "/") and bool(a.path)


def _volume_score(volume) -> float:
    """Missing volume is unknown — do not treat it as zero search demand."""
    if isinstance(volume, (int, float)):
        return min(float(volume), 5000) / 1000
    return 0.5  # neutral when the provider omitted volume


def ranked_on_url(ranked: list, url: str) -> list[dict]:
    """Keywords Searchify SEO already associates with this exact URL."""
    hits = [
        row for row in ranked or []
        if isinstance(row, dict) and row.get("keyword") and _same_page(str(row.get("url") or ""), url)
    ]

    def sort_key(row):
        vol = row.get("volume")
        vol_rank = -(vol if isinstance(vol, (int, float)) else -1)
        pos = row.get("position")
        pos_rank = pos if isinstance(pos, (int, float)) else 10**6
        return (vol_rank, pos_rank)

    hits.sort(key=sort_key)
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
    topical.sort(
        key=lambda row: -(row.get("volume") if isinstance(row.get("volume"), (int, float)) else -1)
    )
    return topical[:5]


def idea_keywords(
    stored: dict | None,
    topic: str,
    offers: list[str] | None = None,
    places: list[str] | None = None,
    limit: int = 8,
) -> list[dict]:
    """Stored keyword ideas that still fit this page topic or the business offers."""
    ideas = ((stored or {}).get("keywords") or {}).get("ideas") or []
    rows = []
    seen: set[str] = set()
    for row in ideas:
        if not isinstance(row, dict):
            continue
        keyword = " ".join(str(row.get("keyword") or "").split())
        key = keyword.lower()
        if not key or key in seen:
            continue
        if not keyword_fits_page(keyword, topic=topic, page_text=topic, offers=offers, places=places):
            continue
        fit = max(
            _keyword_fit(keyword, topic, topic),
            _keyword_fit(keyword, " ".join(offers or []), " ".join(offers or [])) if offers else 0.0,
        )
        seen.add(key)
        rows.append({**row, "keyword": keyword, "_fit": fit, "provenance": "dataforseo_idea"})
    rows.sort(
        key=lambda row: (
            -(row.get("volume") if isinstance(row.get("volume"), (int, float)) else -1),
            -row.get("_fit", 0),
        )
    )
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
    named = [row for row in checks if row.get("mention") is True]
    missed = [row for row in checks if row.get("mention") is False]
    unchecked = [row for row in checks if row.get("mention") is None]
    if missed and not named:
        engines = ", ".join(sorted({str(row.get("engine") or "answer engine") for row in missed[:4]}))
        return (
            f"AI visibility (live stored checks): the brand was not named on {engines}. "
            "State the offer clearly on this page; do not claim a citation is guaranteed."
        )
    if named:
        return f"AI visibility (live stored checks): named in {len(named)} of {len(checks)} checks. Keep the offer wording clear."
    if unchecked:
        return "AI visibility: stored checks exist but mention status was not determined. Do not treat this as an observed citation."
    return ""


def keyword_fits_page(
    term: str,
    *,
    topic: str = "",
    page_text: str = "",
    offers: list[str] | None = None,
    places: list[str] | None = None,
) -> bool:
    """True when a research or GSC term shares tokens with this page's subject.

    A shared city/region name alone is not enough — place tokens are stripped before matching.
    """
    text = " ".join(str(term or "").split())
    if not text:
        return False
    offer_blob = " ".join(str(item) for item in (offers or []) if str(item).strip())
    place_tokens: set[str] = set()
    for place in places or []:
        place_tokens |= _content_tokens(str(place))
    subject = " ".join(part for part in (topic, offer_blob) if part)
    if not subject.strip() and not (page_text or "").strip():
        return True
    term_core = _content_tokens(text) - place_tokens
    subject_core = (_content_tokens(subject) | _content_tokens(page_text)) - place_tokens
    if not term_core:
        return False
    if term_core & subject_core:
        return True
    if term_core & _content_tokens(offer_blob):
        return True
    return max(
        _keyword_fit(" ".join(sorted(term_core)), " ".join(sorted(subject_core - _content_tokens(page_text))), page_text or topic),
        _keyword_fit(" ".join(sorted(term_core)), offer_blob, page_text or offer_blob) if offer_blob else 0.0,
    ) >= 0.5


def best_keyword_from_research(
    stored: dict | None,
    *,
    url: str,
    topic: str,
    offers: list[str] | None = None,
    queries: list[str] | None = None,
    page_text: str = "",
    places: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Best keyword for this page from stored Searchify SEO rows, then Search Console queries.

    Returns (best, alternatives). Alternatives keep order of score. Each candidate must
    still fit the page topic / offers / page text when that context is available.
    """
    keywords = (stored or {}).get("keywords") or {}
    ranked = keywords_for_page(keywords.get("ranked") or [], url, topic)
    ideas = idea_keywords(stored, topic, offers, places=places)
    scored: list[tuple[float, str, str]] = []
    seen: set[str] = set()

    def add(term: str, score: float, provenance: str) -> None:
        text = " ".join(str(term or "").split())[:80]
        key = text.lower()
        if not text or key in seen:
            return
        if not keyword_fits_page(text, topic=topic, page_text=page_text, offers=offers, places=places):
            return
        seen.add(key)
        scored.append((score, text, provenance))

    for index, row in enumerate(ranked):
        on_page = 3.0 if _same_page(str(row.get("url") or ""), url) else 1.0
        position = row.get("position") if isinstance(row.get("position"), (int, float)) else 50
        add(
            row.get("keyword"),
            on_page + _volume_score(row.get("volume")) + max(0, 20 - float(position)) / 10 - index * 0.05,
            "dataforseo_ranked",
        )
    for index, row in enumerate(ideas):
        add(
            row.get("keyword"),
            1.2 + _volume_score(row.get("volume")) * 0.83 + row.get("_fit", 0) - index * 0.04,
            "dataforseo_idea",
        )
    for index, query in enumerate(queries or []):
        add(query, 0.8 - index * 0.05, "search_console")
    for offer in offers or []:
        add(offer, 0.6, "business_offer")
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return "", []
    return scored[0][1], [text for _score, text, _prov in scored[:6]]


def research_lines(stored: dict | None, url: str, topic: str, offers: list[str] | None = None) -> str:
    data = stored or {}
    keywords = data.get("keywords") or {}
    ranked = keywords_for_page(keywords.get("ranked") or [], url, topic)
    ideas = idea_keywords(data, topic, offers)
    lines = []
    if ranked:
        lines.append(
            "Searchify SEO keywords for this page (DataForSEO estimates — volume/position are not Search Console):"
        )
        for row in ranked:
            volume = row.get("volume") if row.get("volume") is not None else "—"
            position = row.get("position") if row.get("position") is not None else "—"
            lines.append(f"- {row.get('keyword')} · volume {volume} · position {position} · source dataforseo_ranked")
    else:
        lines.append("Searchify SEO has no stored ranked keyword that matches this page yet.")
    if ideas:
        lines.append("Stored keyword ideas that still fit this page (DataForSEO ideas, not observed rankings):")
        for row in ideas[:5]:
            volume = row.get("volume") if row.get("volume") is not None else "—"
            lines.append(f"- {row.get('keyword')} · volume {volume} · source dataforseo_idea")
    summary = (data.get("backlinks") or {}).get("summary") or {}
    if summary:
        fetched = (data.get("backlinks") or {}).get("fetchedAt") or "—"
        lines.append(
            "Backlinks for the domain (DataForSEO index"
            f", fetched {fetched}): "
            f"referring domains {summary.get('referringDomains') if summary.get('referringDomains') is not None else '—'}, "
            f"domain rank {summary.get('rank') if summary.get('rank') is not None else '—'}."
        )
        anchors = page_anchors(data, url)
        if anchors:
            lines.append("Anchors already pointing at this page: " + "; ".join(anchors))
        else:
            lines.append("No page-level referring anchors matched this URL in the stored sample.")
    else:
        lines.append("No stored backlink summary yet.")
    gap = visibility_gap(data)
    checks = data.get("visibility") or []
    if checks:
        lines.append("AI visibility live checks already stored (not an estimate report):")
        for row in checks[:4]:
            mention = row.get("mention")
            if mention is True:
                mentioned = "mentioned"
            elif mention is False:
                mentioned = "not mentioned"
            else:
                mentioned = "not checked"
            engine = row.get("engine") or "an answer engine"
            snippet = " ".join(str(row.get("snippet") or row.get("text") or "").split())[:140]
            lines.append(f"- {engine}: brand {mentioned}. {snippet}")
        if gap:
            lines.append(gap)
    else:
        lines.append("No stored live AI visibility check yet.")
    audit = data.get("audit") or {}
    report = audit.get("report") if isinstance(audit.get("report"), dict) else {}
    findings = report.get("issues") or audit.get("issues") or audit.get("findings") or []
    if isinstance(findings, list) and findings:
        lines.append("Stored DataForSEO on-page audit notes (not PageSpeed). Do not invent severity:")
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
