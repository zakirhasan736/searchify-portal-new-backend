"""JEV — Judge / Evaluate / Verify gate for OpenAI drafts before human publish."""

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
