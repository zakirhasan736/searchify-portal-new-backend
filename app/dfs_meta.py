"""Searchify SEO content generation for meta titles and descriptions.

Uses the paid content_generation/generate_meta_tags endpoint on a packed page brief.
Fails soft when the provider is off, out of credit, or returns nothing usable.
"""

from __future__ import annotations

from app import research


def pack_source_text(
    *,
    keyword: str = "",
    topic: str = "",
    offers: list[str] | None = None,
    current_title: str = "",
    current_desc: str = "",
    page_text: str = "",
    headings: str = "",
    rival_title: str = "",
    anchors: list[str] | None = None,
    visibility_gap: str = "",
) -> str:
    """Build the short source text Searchify SEO needs to draft meta tags."""
    parts = []
    if keyword:
        parts.append(f"Primary search keyword: {keyword}.")
    if topic:
        parts.append(f"Page topic: {topic}.")
    if offers:
        parts.append("Business offers: " + ", ".join(str(item) for item in offers[:6] if str(item).strip()) + ".")
    if current_title:
        parts.append(f"Current title: {current_title}.")
    if current_desc:
        parts.append(f"Current description: {current_desc}.")
    if rival_title:
        parts.append(f"Strong competitor listing to meet on the same keyword (do not copy): {rival_title}.")
    if anchors:
        parts.append("Existing link anchors to this page: " + "; ".join(anchors[:4]) + ".")
    if visibility_gap:
        parts.append(visibility_gap)
    body = " ".join(str(page_text or "").split())
    if headings:
        parts.append("Headings: " + " | ".join(str(headings).split("|")[:8]))
    if body:
        parts.append(body[:1800])
    text = "\n".join(parts)
    return text[:4500]


def generate_meta_tags(text: str, *, creativity: float = 0.75) -> dict:
    """Call Searchify SEO content generation. Returns {title, description, cost} or {}."""
    source = " ".join(str(text or "").split())
    if len(source) < 40:
        return {}
    try:
        result, cost = research._call(
            "POST",
            "/content_generation/generate_meta_tags/live",
            [{"text": source[:4500], "creativity": max(0.1, min(1.0, float(creativity)))}],
            timeout=45,
        )
    except research.ResearchError:
        return {}
    row = (result or [{}])[0] or {}
    title = " ".join(str(row.get("title") or "").split())
    description = " ".join(str(row.get("description") or "").split())
    if not title or not description:
        return {}
    return {"title": title[:80], "description": description[:200], "cost": float(cost or 0), "source": "searchify-seo-content"}


def draft_from_research(
    db,
    user,
    *,
    keyword: str,
    topic: str,
    offers: list[str] | None,
    current_title: str,
    current_desc: str,
    page_text: str,
    headings: str,
    rival_title: str,
    anchors: list[str] | None,
    visibility_gap: str,
) -> dict:
    """Quota-aware meta draft from Searchify SEO. Empty when the call should not run."""
    if db is None or user is None or not research.configured():
        return {}
    from app import quotas

    try:
        quotas.check(db, user, "content")
        research._budget(db, user, 1)
    except (quotas.QuotaError, research.ResearchError):
        return {}
    packed = pack_source_text(
        keyword=keyword,
        topic=topic,
        offers=offers,
        current_title=current_title,
        current_desc=current_desc,
        page_text=page_text,
        headings=headings,
        rival_title=rival_title,
        anchors=anchors,
        visibility_gap=visibility_gap,
    )
    drafted = generate_meta_tags(packed)
    if not drafted:
        return {}
    try:
        research._spend(db, user, 1, drafted.get("cost") or 0)
        quotas.consume(db, user, "content")
    except Exception:  # noqa: BLE001
        pass
    return drafted
