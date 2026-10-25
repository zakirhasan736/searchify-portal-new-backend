"""Independent metadata evaluator. It does not generate copy and does not trust an SEO score."""

from __future__ import annotations

import re

_GENERIC_TITLE = re.compile(r"^(home|welcome|untitled|index|page)(\b|\s|\|)", re.I)
_FABRICATED_METRIC = re.compile(
    r"\b(\d[\d,]*\s*\+?\s*(monthly\s+)?(searches|search volume|impressions|clicks)|"
    r"\d{1,3}\s*%\s*(traffic|ctr|growth)|rank(?:ed|ing)?\s*#?\s*\d+)\b",
    re.I,
)


def _tokens(value: str) -> set[str]:
    return {part for part in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(part) > 2}


def _brand_dropped(current: str, candidate: str, brand: str) -> bool:
    name = " ".join(str(brand or "").split())
    if not name or len(name) < 2:
        return False
    if name.lower() not in (current or "").lower():
        return False
    return name.lower() not in (candidate or "").lower()


def _generic_title(title: str) -> bool:
    text = " ".join(str(title or "").split())
    if not text or len(text) < 12:
        return True
    return bool(_GENERIC_TITLE.match(text))


def _supported_by_page(candidate: str, page_text: str, offers: list[str], keyword: str) -> bool:
    words = _tokens(candidate)
    source = _tokens(f"{page_text} {' '.join(offers)} {keyword}")
    if not words:
        return False
    return len(words & source) >= min(2, len(words))


def _query_support(candidate: str, queries: list[dict], keyword: str) -> bool:
    blob = _tokens(f"{candidate} {keyword}")
    for row in queries or []:
        query_words = _tokens(str(row.get("query") or ""))
        if query_words and len(query_words & blob) >= max(1, (len(query_words) + 1) // 2):
            return True
    return False


def evaluate_metadata(
    *,
    evidence: dict,
    current_title: str,
    current_description: str,
    candidates: list[dict],
    brand: str = "",
    offers: list[str] | None = None,
    page_text: str = "",
    keyword: str = "",
    taken_titles: list[str] | None = None,
) -> dict:
    """Score the current listing and each candidate on the same evidence.

    Outcomes: RECOMMEND_CHANGE, KEEP_CURRENT, INSUFFICIENT_EVIDENCE.
    A candidate can also be REJECT_CANDIDATE without becoming the recommendation.
    """
    from app.meta_writer import listing_fluff_problems, listing_is_stronger
    from app.page_quality import validate_claims

    offers = [str(item).strip() for item in (offers or []) if str(item).strip()]
    search = (evidence or {}).get("search_console") or {}
    dfs = (evidence or {}).get("dataforseo") or {}
    technical = (evidence or {}).get("technical_audit") or {}
    page_verified = technical.get("status") == "verified" and bool((current_title or "").strip() or page_text.strip())
    gsc_status = search.get("status") or "not_verified"
    dfs_status = dfs.get("status") or "not_verified"
    page_queries = search.get("queries") or []
    dfs_keywords = dfs.get("page_keywords") or []
    warnings = list((evidence or {}).get("data_quality_warnings") or [])
    rejected = []
    accepted = []

    def reject(candidate: dict, reasons: list[str]) -> None:
        rejected.append({
            "title": candidate.get("title") or "",
            "description": candidate.get("description") or "",
            "outcome": "REJECT_CANDIDATE",
            "reasons": reasons,
        })

    for candidate in candidates or []:
        title = " ".join(str(candidate.get("title") or "").split())
        description = " ".join(str(candidate.get("description") or "").split())
        if not title or not description:
            continue
        if title.lower() == (current_title or "").strip().lower() and description.lower() == (current_description or "").strip().lower():
            continue
        reasons = []
        if _brand_dropped(current_title, title, brand):
            reasons.append("The candidate removes the brand without a defensible reason.")
        fluff = listing_fluff_problems(title, description, evidence=page_text or current_title)
        reasons.extend(fluff)
        claims = validate_claims(title, description, evidence=page_text or current_title, places=[])
        for claim in claims.get("claims") or []:
            if claim.get("status") == "contradicted":
                reasons.append(f"“{claim.get('claim')}” contradicts the page.")
            elif claim.get("status") == "unverified" and claim.get("kind") in {"number", "promise"}:
                reasons.append(f"“{claim.get('claim')}” is not supported by the page.")
        if _FABRICATED_METRIC.search(f"{title} {description}") and not _FABRICATED_METRIC.search(page_text or ""):
            reasons.append("The candidate states a search or performance figure that is not in the evidence.")
        if not _supported_by_page(title, page_text, offers, keyword):
            reasons.append("The candidate is not grounded in this page's content or offers.")
        for other in taken_titles or []:
            other_words = _tokens(other)
            title_words = _tokens(title)
            if other_words and title_words:
                overlap = len(other_words & title_words) / len(other_words | title_words)
                if overlap >= 0.85 or title.strip().lower() == other.strip().lower():
                    reasons.append(f"The title duplicates another page: {other}")
                    break
        if gsc_status in {"mismatched", "stale", "error"}:
            reasons.append(f"Search Console evidence is {gsc_status}, so it cannot support a change.")
        if reasons:
            reject(candidate, reasons[:6])
            continue
        query_backed = gsc_status == "verified" and _query_support(title, page_queries, keyword)
        dfs_backed = dfs_status == "verified" and _query_support(title, [{"query": row.get("keyword")} for row in dfs_keywords], keyword)
        repairs_generic = _generic_title(current_title) and _supported_by_page(title, page_text, offers, keyword)
        stronger = listing_is_stronger(
            title,
            description,
            current_title,
            current_description,
            brand,
            keyword=keyword,
            offers=offers,
        )
        if not (query_backed or dfs_backed or repairs_generic):
            reject(candidate, ["No verified page query, DataForSEO keyword, or broken-title repair supports this change."])
            continue
        if current_title.strip() and not stronger and not repairs_generic:
            reject(candidate, ["The candidate does not show a material improvement over the current title."])
            continue
        accepted.append({
            "title": title,
            "description": description,
            "query_backed": query_backed,
            "dfs_backed": dfs_backed,
            "repairs_generic": repairs_generic,
        })

    recommended = accepted[0] if accepted else None
    fabricated = any(
        "figure" in " ".join(item.get("reasons") or []).lower() or "not supported" in " ".join(item.get("reasons") or []).lower()
        for item in rejected
    )
    search_blocked = gsc_status in {"not_verified", "error", "mismatched", "stale", "partial"} and dfs_status != "verified"
    if recommended and page_verified:
        decision = "RECOMMEND_CHANGE"
        confidence = "HIGH" if recommended["query_backed"] else "MEDIUM"
        summary = (
            "The candidate is more specific than the current metadata and is supported by "
            + (
                "page Search Console queries"
                if recommended["query_backed"]
                else "verified DataForSEO keywords"
                if recommended["dfs_backed"]
                else "the live page, because the current title does not describe it"
            )
            + "."
        )
    elif gsc_status in {"mismatched", "stale", "error"}:
        decision = "INSUFFICIENT_EVIDENCE"
        confidence = "LOW"
        summary = "Search Console data is stale, failed, or tied to a different URL, so an evidence-dependent change is blocked."
        recommended = None
    elif not page_verified or (search_blocked and (fabricated or _generic_title(current_title) or not (current_title or "").strip())):
        decision = "INSUFFICIENT_EVIDENCE"
        confidence = "LOW"
        summary = "Required page or search evidence is missing, stale, or not reliable enough to recommend a metadata change."
        recommended = None
    elif search_blocked and fabricated and not (current_title or "").strip():
        decision = "INSUFFICIENT_EVIDENCE"
        confidence = "LOW"
        summary = "Search evidence is missing, and the candidate includes measurements that were not supplied."
        recommended = None
    else:
        decision = "KEEP_CURRENT"
        confidence = "MEDIUM" if page_verified else "LOW"
        summary = (
            "No candidate shows a material, evidence-backed advantage over the current title and description. "
            "Keep the published metadata."
        )
        recommended = None
    if decision != "RECOMMEND_CHANGE" and search_blocked and fabricated:
        decision = "INSUFFICIENT_EVIDENCE"
        confidence = "LOW"
        summary = (
            "Search Console or DataForSEO evidence for this page is not verified, and a candidate used unsupported measurements. "
            "No insight was fabricated from that gap."
        )
        recommended = None
    return {
        "decision": decision,
        "analysis_status": "INSUFFICIENT_EVIDENCE" if decision == "INSUFFICIENT_EVIDENCE" else "READY",
        "recommended_title": recommended["title"] if recommended else None,
        "recommended_description": recommended["description"] if recommended else None,
        "rejected": rejected,
        "accepted_count": len(accepted) if decision == "RECOMMEND_CHANGE" else 0,
        "comparison_summary": summary,
        "confidence": confidence,
        "human_approval_required": True,
        "data_quality_warnings": warnings,
        "criteria": [
            "relevance",
            "search_intent",
            "evidence",
            "brand",
            "clarity",
            "duplication",
            "consistency",
            "confidence",
        ],
    }
