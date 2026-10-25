"""Best-of-N listing tournament: many candidates, strict fitting, and a blind judge against the live listing.

Why this exists
---------------
The old flow wrote ONE draft (plus one retry), chopped it to 60/160 characters mid-phrase, and then
decided "better than live" with token-overlap heuristics. That produced three failure modes:

1. The model was told to echo the live listing when it already named brand/offer/place, so most runs
   simply returned the current title.
2. Drafts that were genuinely better were rejected by lexical rules (overlap >= 0.75, offer counts),
   while chopped drafts ("Web Design, SEO & Google Ads in") were sometimes accepted.
3. Nothing ever compared quality the way a searcher reads a result.

This module generates several distinct candidates, fits them without breaking phrases, and asks a
separate judge pass to score the live listing and every candidate *blind* (shuffled, unlabeled) on
the same rubric. A candidate only wins when it beats the live listing by a clear margin.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from app.openai_client import chat_text

log = logging.getLogger(__name__)

TITLE_MAX = 60
DESC_MAX = 160
TITLE_PX_MAX = 580  # Google desktop title truncation is ~580–600px at 20px Arial.
WIN_MARGIN = 4.0  # On a 0–100 scale the winner must clear the live listing by this much.

# ---------------------------------------------------------------------------
# Generation prompt
# ---------------------------------------------------------------------------

GENERATOR_SYSTEM = (
    "You are the best SEO copywriter in the world, writing the Google title tag and meta description for ONE page. "
    "Your job is to write listings that beat the current live listing on relevance AND click appeal. "
    "Echoing the live listing is a failure; small reshuffles of it are a failure.\n\n"
    "What a winning listing does:\n"
    "- Title: the best keyword (or its natural variant) in the first 3–5 words, then the most persuasive page-true "
    "differentiator (place, audience, specific service, concrete benefit), then the brand at the end after ' | '. "
    "Reads like a human headline, not a keyword list. 50–60 characters. Never more than 60.\n"
    "- Description: 140–158 characters, one or two complete sentences. Lead with what the searcher gets, add one concrete "
    "page-true detail that competitors cannot claim, end with a soft action (e.g. 'Get a quote', 'See plans', "
    "'Book a call') only if the page offers it. Use a secondary keyword naturally. Never repeat the title.\n"
    "- Matches search intent: commercial pages sell the offer, homepages state what the business is and where, "
    "articles promise the answer.\n\n"
    "Hard rules:\n"
    "- Use only facts in the brief. Never invent prices, years, counts, awards, reviews, guarantees, or results.\n"
    "- No filler: welcome, discover, unlock, elevate, seamless, cutting-edge, leading, world-class, one-stop, "
    "trusted, look no further, best-in-class.\n"
    "- Keep the brand when the live title has it. Shorten other words instead.\n"
    "- Stay on this page's topic. Do not name portfolio clients on home or service pages.\n"
    "- Count characters. Titles over 60 or descriptions over 160 are thrown away, not shortened.\n\n"
    "Write 5 genuinely different candidates, each with a different lead angle:\n"
    "1 keyword-first, 2 benefit/outcome-first, 3 audience or place-specific, 4 differentiator-first, "
    "5 intent-matched (how the searcher phrases the need).\n\n"
    "Return only this format, nothing else:\n"
    "TITLE: ...\nDESCRIPTION: ...\n"
    "(repeat for all 5)\n"
    "TARGET: the best keyword these listings are written to win\n"
    "REASON: one sentence on what the live listing is missing that these fix."
)

REFINE_INSTRUCTION = (
    "\n\nA blind reviewer scored the live listing and the drafts. None beat the live listing by enough.\n"
    "Live listing scored {live_score}/100. Reviewer notes on the live listing: {live_note}\n"
    "Best draft so far ({best_score}/100):\nTITLE: {best_title}\nDESCRIPTION: {best_desc}\n"
    "Reviewer notes on it: {best_note}\n\n"
    "Write 3 new candidates that keep everything the live listing does well AND fix its weaknesses. "
    "Do not repeat any earlier draft. Same format: TITLE/DESCRIPTION pairs, then TARGET and REASON."
)

# ---------------------------------------------------------------------------
# Strict fitting — never chop a phrase in half
# ---------------------------------------------------------------------------

_SEPS = (" | ", " – ", " — ", " - ", ": ")
_DANGLING = re.compile(r"(?:\b(?:and|or|for|in|of|with|to|the|a|an|&|\+|at|by|on))\s*$", re.I)

# Rough Arial 20px glyph widths (px). Good enough to flag titles Google will truncate.
_NARROW = set("fijlrtI.,;:!'|() ")
_WIDE = set("mwMWOQGDCHNU@%")


def title_pixels(text: str) -> int:
    width = 0.0
    for char in text or "":
        if char in _NARROW:
            width += 5.5
        elif char in _WIDE:
            width += 15.5
        elif char.isupper():
            width += 12.5
        elif char.isdigit():
            width += 11.0
        else:
            width += 10.0
    return int(width)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().strip("\"'`")).strip()


def _split_brand(title: str, brand_tail: str) -> tuple[str, str, str]:
    """(head, separator, brand) when the title ends with the brand; otherwise (title, '', '')."""
    tail = (brand_tail or "").strip().lower()
    tail_words = set(re.findall(r"[a-z0-9]+", tail)) - {"inc", "llc", "ltd", "co", "corp", "company"}
    for sep in _SEPS:
        if sep in title:
            left, right = title.rsplit(sep, 1)
            right_words = set(re.findall(r"[a-z0-9]+", right.lower()))
            if not tail or right.strip().lower() == tail or (tail_words and tail_words <= right_words):
                if tail or len(right.split()) <= 4:
                    return left.strip(), sep, right.strip()
    return title, "", ""


def fit_title_strict(title: str, brand_tail: str = "") -> str:
    """Return a title within 60 chars / ~580px without cutting a phrase. '' when it cannot fit cleanly."""
    text = _norm(title).rstrip(" |–—-:")
    if not text:
        return ""

    def ok(value: str) -> bool:
        return len(value) <= TITLE_MAX and title_pixels(value) <= TITLE_PX_MAX + 20 and not _DANGLING.search(value)

    if ok(text):
        return text
    head, sep, brand = _split_brand(text, brand_tail)
    suffix = f"{sep}{brand}" if brand else ""
    # Drop middle " | " segments of the head, right to left.
    segments = [part.strip() for part in re.split(r"\s+\|\s+|\s+–\s+|\s+—\s+", head) if part.strip()]
    while len(segments) > 1:
        segments.pop()
        trial = " | ".join(segments) + suffix
        if ok(trial):
            return trial
    lead = segments[0] if segments else head
    # Drop trailing list items: "A, B & C" -> "A & B" -> "A".
    items = [part.strip() for part in re.split(r",\s*|\s+&\s+|\s+and\s+", lead) if part.strip()]
    while len(items) > 1:
        items.pop()
        joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " & " + items[-1]
        trial = joined + suffix
        if ok(trial):
            return trial
    return ""


def fit_desc_strict(desc: str) -> str:
    """Return a description within 160 chars ending on a full sentence. '' when it cannot fit cleanly."""
    text = _norm(desc)
    if not text:
        return ""
    if len(text) <= DESC_MAX:
        return text
    window = text[: DESC_MAX + 1]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if window.endswith((".", "!", "?")):
        cut = max(cut, len(window) - 1)
    if cut >= 100:
        return window[: cut + 1].strip()
    # One long sentence: end it at the last clause break that keeps a useful description.
    room = text[: DESC_MAX]
    for marker in (" — ", " – ", "; ", ", ", " with ", " and "):
        at = room.rfind(marker)
        if at >= 110:
            clause = room[:at].rstrip(" ,;:–—-")
            if not _DANGLING.search(clause):
                return clause + "."
    return ""


# ---------------------------------------------------------------------------
# Blind judge
# ---------------------------------------------------------------------------

RUBRIC = {
    "relevance": 2.0,  # matches this page's real content and search intent
    "keyword": 2.0,  # best keyword (or natural variant) present, early, natural
    "click_appeal": 2.0,  # a searcher would choose it over the competitors listed
    "specificity": 1.5,  # concrete page-true detail, not generic
    "brand_trust": 1.0,  # brand present where useful, credible tone
    "readability": 1.0,  # human, scannable, fits without truncation, title/desc complement
    "accuracy": 0.5,  # every claim supported by the page (also a gate)
}
_RUBRIC_TOTAL = sum(RUBRIC.values()) * 10

JUDGE_SYSTEM = (
    "You are a strict senior SEO reviewer. You score Google search listings (title + meta description) for one page. "
    "You do not know which listing is currently live; judge each on its own merits against the page evidence. "
    "Score each criterion 0–10:\n"
    "- relevance: describes THIS page and matches the searcher's intent\n"
    "- keyword: the target keyword or a natural variant appears early in the title and reads naturally\n"
    "- click_appeal: next to the competitor titles, a searcher would pick this one; clear benefit in the first words\n"
    "- specificity: includes a concrete, page-true detail (place, audience, service, outcome) instead of generic wording\n"
    "- brand_trust: brand present where useful, credible, no hype\n"
    "- readability: natural headline, no keyword stuffing, title and description complement each other, no truncation\n"
    "- accuracy: every claim is supported by the page evidence (score 0–4 if anything is invented)\n"
    "Be decisive: use the full range. A listing that merely restates the H1 gets low click_appeal and specificity.\n"
    "Return ONLY JSON: {\"items\": [{\"id\": \"L1\", \"relevance\": 0, \"keyword\": 0, \"click_appeal\": 0, "
    "\"specificity\": 0, \"brand_trust\": 0, \"readability\": 0, \"accuracy\": 0, \"note\": \"one short sentence: main weakness\"}]}"
)


def weighted_total(scores: dict) -> float:
    total = 0.0
    for key, weight in RUBRIC.items():
        try:
            value = float(scores.get(key, 0))
        except (TypeError, ValueError):
            value = 0.0
        total += max(0.0, min(10.0, value)) * weight
    return round(100 * total / _RUBRIC_TOTAL, 1)


def _order(count: int, salt: str) -> list[int]:
    """Deterministic shuffle so the live listing is not always first."""
    keyed = [(hashlib.sha1(f"{salt}:{i}".encode()).hexdigest(), i) for i in range(count)]
    return [i for _key, i in sorted(keyed)]


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge returned no JSON")
    return json.loads(raw[start : end + 1])


def judge_listings(evidence: str, listings: list[dict], *, salt: str = "") -> list[dict] | None:
    """Score listings blind. listings[i] = {"title","description"}. Returns same order with score/note, or None."""
    if not listings:
        return []
    order = _order(len(listings), salt or listings[0].get("title", ""))
    labels = {}
    lines = []
    for position, index in enumerate(order, start=1):
        label = f"L{position}"
        labels[label] = index
        row = listings[index]
        lines.append(
            f"{label}\n  title ({len(row['title'])} chars): {row['title']}\n"
            f"  description ({len(row['description'])} chars): {row['description']}"
        )
    user = f"PAGE EVIDENCE\n{evidence}\n\nLISTINGS TO SCORE\n" + "\n".join(lines)
    try:
        text, _model, _role = chat_text(
            system=JUDGE_SYSTEM,
            user=user,
            kind="meta-seo",
            writing_type="Meta listing review",
            tokens=900,
            max_tokens=3000,
        )
        data = _extract_json(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("listing judge unavailable, falling back to heuristic rules: %s", exc)
        return None
    out: list[dict | None] = [None] * len(listings)
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        index = labels.get(str(item.get("id") or "").strip().upper())
        if index is None:
            continue
        scores = {key: item.get(key, 0) for key in RUBRIC}
        try:
            accuracy = float(scores.get("accuracy") or 0)
        except (TypeError, ValueError):
            accuracy = 0.0
        out[index] = {
            **listings[index],
            "scores": scores,
            "score": weighted_total(scores),
            "accurate": accuracy >= 6,
            "note": str(item.get("note") or "")[:200],
        }
    # The live listing (index 0 when present) must be scored; unscored drafts are dropped, not fatal.
    if out[0] is None:
        log.warning("listing judge skipped the first listing; falling back to heuristic rules")
        return None
    return [
        row if row is not None else {**listings[i], "scores": {}, "score": 0.0, "accurate": False, "note": "not scored"}
        for i, row in enumerate(out)
    ]


def judge_evidence(
    *,
    url: str,
    kind: str,
    topic: str,
    keyword: str,
    brand: str,
    offers: list[str],
    places: list[str],
    h1: str,
    headings: str,
    page_text: str,
    queries: list[str],
    rival_titles: list[str],
    research: str = "",
    search_console: str = "",
) -> str:
    return "\n".join(
        [
            f"Keyword research (DataForSEO, matched to this page):\n{research.strip()[:900] or '(none)'}",
            f"Search Console for this URL: {search_console or '(not connected)'}",
            f"URL: {url}",
            f"Page type: {kind or 'page'}",
            f"Page topic: {topic or '(unknown)'}",
            f"Target keyword: {keyword or '(none)'}",
            f"Brand: {brand or '(none)'}",
            f"Offers on site: {', '.join(offers[:8]) or '(none)'}",
            f"Places: {', '.join(places[:6]) or '(none)'}",
            f"H1: {h1 or '(none)'}",
            f"Headings: {str(headings or '')[:300] or '(none)'}",
            f"Search Console queries for this page/site: {', '.join(queries[:8]) or '(none)'}",
            "Competitor titles on the same search:\n" + ("\n".join(f"- {t}" for t in rival_titles[:5]) or "- (none)"),
            f"Page copy (excerpt): {' '.join(str(page_text or '').split())[:1400]}",
        ]
    )
