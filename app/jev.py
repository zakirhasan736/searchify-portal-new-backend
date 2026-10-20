"""JEV — Judge / Evaluate / Verify gate for OpenAI drafts before human publish.

JEV is Searchify's internal quality gate (not a third-party API). DataForSEO supplies
market/keyword/competitor/content research; JEV scores the draft before a human approves.
"""

from __future__ import annotations

import json
import re

from app.openai_client import chat_text, openai_configured

# Product thresholds (0–100)
AUTO_SAFE = 82
INBOX = 55


def score_draft(kind: str, brief: str, body: str) -> dict:
    """
    Returns { score: int, band: auto|inbox|reject, reasons: [...], checks: {...} }.
    Uses OpenAI when available; otherwise heuristic rules.
    """
    if not body.strip():
        return {
            "score": 0,
            "band": "reject",
            "reasons": ["Empty draft"],
            "checks": {"length": False, "headings": False, "placeholders_ok": True},
        }

    heuristic = _heuristic(kind, brief, body)
    if not openai_configured():
        return heuristic

    try:
        text, _, _ = chat_text(
            system=(
                "You are JEV, Searchify's draft judge. Score SEO drafts 0-100. "
                "Return ONLY compact JSON: "
                '{"score":0-100,"reasons":["..."],"checks":{"clarity":true,"seo_safe":true,"no_fake_metrics":true,"actionable":true}}'
            ),
            user=(
                f"Kind: {kind}\nBrief:\n{brief[:800]}\n\nDraft:\n{body[:3500]}\n\n"
                "Penalize invented traffic/rank/backlink numbers. Reward clear structure and usable SEO copy."
            ),
            kind="meta",
            brief=brief[:200],
            tokens=200,
            max_tokens=400,
        )
        parsed = _parse_json(text)
        if parsed and "score" in parsed:
            score = int(parsed["score"])
            score = max(0, min(100, score))
            # Blend lightly with heuristic so empty junk can't game the model
            score = int(round(score * 0.75 + heuristic["score"] * 0.25))
            return {
                "score": score,
                "band": _band(score),
                "reasons": list(parsed.get("reasons") or heuristic["reasons"])[:5],
                "checks": {**heuristic["checks"], **(parsed.get("checks") or {})},
            }
    except Exception:  # noqa: BLE001
        pass
    return heuristic


def score_listing(
    *,
    title: str,
    description: str,
    keyword: str = "",
    live_title: str = "",
    live_description: str = "",
    beats_live: bool = False,
    meet_competitor: str = "",
    open_issues: list | None = None,
) -> dict:
    """JEV for a Google listing package (title + description + OG/JSON-LD same words).

    Tuned for short SERP copy — not long articles. Still returns band auto|inbox|reject.
    Human approval remains required to publish; JEV only grades the draft.
    """
    title = (title or "").strip()
    description = (description or "").strip()
    if not title or not description:
        return {
            "score": 0,
            "band": "reject",
            "reasons": ["Listing package is missing a title or description"],
            "checks": {
                "has_title": bool(title),
                "has_description": bool(description),
                "keyword_in_title": False,
                "beats_live": False,
                "length_ok": False,
                "not_live_paste": False,
                "placeholders_ok": True,
            },
        }

    # Listing JEV is heuristic-first so regenerate stays fast. Long-form drafts still use OpenAI JEV.
    return _listing_heuristic(
        title=title,
        description=description,
        keyword=keyword,
        live_title=live_title,
        live_description=live_description,
        beats_live=beats_live,
        meet_competitor=meet_competitor,
        open_issues=open_issues or [],
    )


def _listing_heuristic(
    *,
    title: str,
    description: str,
    keyword: str,
    live_title: str,
    live_description: str,
    beats_live: bool,
    meet_competitor: str,
    open_issues: list,
) -> dict:
    title_len = len(title)
    desc_len = len(description)
    kw = {part for part in re.findall(r"[a-z0-9]+", (keyword or "").lower()) if len(part) > 2}
    title_words = {part for part in re.findall(r"[a-z0-9]+", title.lower()) if len(part) > 2}
    keyword_hit = bool(kw) and len(kw & title_words) >= max(1, (len(kw) + 1) // 2)
    live_overlap = False
    if live_title:
        live_words = {part for part in re.findall(r"[a-z0-9]+", live_title.lower()) if len(part) > 2}
        if live_words and title_words:
            live_overlap = len(live_words & title_words) / len(live_words | title_words) >= 0.75
    fake = bool(
        re.search(
            r"\b(\d{1,3}\s*%\s*(traffic|ctr|share)|rank(ed|ing)?\s*#?\s*\d|monthly searches?:\s*\d+)\b",
            f"{title} {description}",
            re.I,
        )
    )
    length_ok = 45 <= title_len <= 65 and 120 <= desc_len <= 160
    score = 48
    reasons = []
    if keyword_hit:
        score += 18
        reasons.append("Keyword leads the title")
    else:
        score -= 12
        reasons.append("Title misses the target keyword")
    if beats_live:
        score += 16
        reasons.append("Beats the live listing")
    elif live_overlap:
        score -= 18
        reasons.append("Too close to the live title")
    else:
        score -= 6
        reasons.append("Not clearly stronger than live yet")
    if length_ok:
        score += 10
    else:
        score -= 4
        reasons.append("Title or description length is off the SERP window")
    if meet_competitor:
        score += 6
        reasons.append("Written against a real competitor")
    if open_issues:
        score -= min(20, 4 * len(open_issues))
        reasons.append("Open draft checks remain")
    if fake:
        score -= 25
        reasons.append("Possible invented metrics")
    if not live_overlap and title.lower() != (live_title or "").lower():
        score += 4
    score = max(0, min(100, score))
    if not reasons:
        reasons.append("Listing heuristic pass")
    return {
        "score": score,
        "band": _band(score),
        "reasons": reasons[:5],
        "checks": {
            "has_title": True,
            "has_description": True,
            "keyword_in_title": keyword_hit,
            "beats_live": bool(beats_live),
            "length_ok": length_ok,
            "not_live_paste": not live_overlap,
            "placeholders_ok": not fake,
            "title_chars": title_len,
            "description_chars": desc_len,
            "meet_competitor": bool(meet_competitor),
        },
    }


def _band(score: int) -> str:
    if score >= AUTO_SAFE:
        return "auto"
    if score >= INBOX:
        return "inbox"
    return "reject"


def _heuristic(kind: str, brief: str, body: str) -> dict:
    words = len(re.findall(r"\w+", body))
    has_heading = bool(re.search(r"(?m)^(#{1,3}\s|[A-Z][A-Za-z0-9 ,\-:]{8,}$)", body)) or "\n\n" in body
    fake = bool(
        re.search(
            r"\b(\d{1,3}\s*%\s*(traffic|ctr|share)|DR\s*\d{2}|monthly searches?:\s*\d{4,})\b",
            body,
            re.I,
        )
    )
    score = 40
    if words >= 120:
        score += 15
    if words >= 300:
        score += 10
    if has_heading:
        score += 10
    if brief and any(w.lower() in body.lower() for w in re.findall(r"[A-Za-z]{4,}", brief)[:8]):
        score += 10
    if kind in {"ai-article", "seo-brief", "seo-writing", "content-optimizer"}:
        score += 5
    if fake:
        score -= 25
    if words < 40:
        score -= 20
    score = max(0, min(100, score))
    reasons = []
    if words < 80:
        reasons.append("Draft is short")
    if fake:
        reasons.append("Possible invented metrics")
    if has_heading:
        reasons.append("Has structure")
    if not reasons:
        reasons.append("Heuristic pass")
    return {
        "score": score,
        "band": _band(score),
        "reasons": reasons,
        "checks": {
            "length": words >= 80,
            "headings": has_heading,
            "placeholders_ok": not fake,
            "word_count": words,
        },
    }


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
