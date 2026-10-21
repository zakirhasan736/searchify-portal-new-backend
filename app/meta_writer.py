"""Page-specific Google title and description. One rewrite if the first draft is generic."""

from __future__ import annotations

import re
from datetime import datetime

from app import page_quality
from app.openai_client import chat_text, openai_configured
from app.page_scrape import scrape_page

FILLER = (
    "welcome to",
    "your trusted",
    "discover ",
    "look no further",
    "unlock ",
    "elevate ",
    "cutting-edge",
    "seamless",
    "best-in-class",
    "world-class",
    "click here",
    "learn more",
    "leading provider",
    "one-stop",
    "solutions for",
    "official website",
    "home page",
    "| home",
    "we offer",
    "we provide",
    "top-rated",
    "number one",
    "#1",
)

SYSTEM = (
    "You are a senior SEO copywriter competing with the best SEO tools. You write the Google title and meta description for one specific page. "
    "The page topic is the concept. The best keyword is the search this page can honestly win. "
    "Work in this order: (1) read the page topic and page type in the brief and stay on that concept — do not switch to another service, product, or case study from elsewhere on the site; "
    "(2) put the best keyword, in those words, near the front of the title when it fits the page; "
    "(3) add one concrete fact that belongs to THIS page only (a place, an offer, or a detail stated in the page copy) so the listing is specific and not a paste of the H1; "
    "(4) meet the hardest competitor on that same search when one is listed. Match keyword strength. Add one fact only this business can say. Do not copy their title; "
    "(5) beat the live title only when an improvement is justified. "
    "If the live title and description already accurately represent this page, say so in REASON and write a listing that preserves that positioning — do not invent a thinner substitute. "
    "Never invent service counts (for example “5 Core Services”), awards, “best/leading/top-rated”, prices, or years unless the page states them. "
    "Do not open the description with filler templates such as “Business owners can explore…” or “Buyers can compare…”. Write a natural sentence about the offer. "
    "A strong listing is conceptual and keyword-clear: a searcher can tell what the page is about in the first words. "
    "Use Search Console queries, Analytics landing facts, site-audit notes, backlink anchors, and AI-visibility gaps only as evidence for which angle to win — never invent numbers. "
    "Every page on the site needs its own topic: never reuse another page's title. "
    "Homepage and service pages describe the business offer. Never name a portfolio client, case-study project, or unrelated product on those pages. "
    "Only a case-study or portfolio page may name the client or project that page is about. "
    "It does not sound like an agency slogan. "
    "Do not invent reviews, years in business, prices, awards, or certifications. "
    "Do not copy a competitor title. "
    "Do not use filler such as welcome, trusted, discover, unlock, elevate, seamless, leading, or one-stop. "
    "Title: about 50–60 characters when practical. Keyword and topic first. Brand at the end when it still fits. "
    "Description: about 140–155 characters when practical. It must not repeat the title word for word. "
    "Explain the actual offering and why it matters to the searcher. "
    "The same title and description are the Open Graph title, the Open Graph description, and the WebPage schema name and description. "
    "Read the current title, meta description, Open Graph, and JSON-LD. Stay on that page's concept. "
    "A one-word H1 such as Home is not the topic — use the schema name, the Open Graph title, or the real heading. "
    "Do not invent search volume, positions, or mentions that are not in the brief. "
    "Do not put click counts, session counts, or bounce rates inside the title or description. "
    "Return exactly four lines:\n"
    "TITLE: ...\n"
    "DESCRIPTION: ...\n"
    "TARGET: the best keyword this listing is written to win\n"
    "REASON: one sentence naming the page topic, the keyword, and whether this improves on the live listing."
)

OBJECTIVES = {
    "balanced": (
        "Optimization objective: balanced SEO improvement. Strengthen clarity and keyword fit without abandoning "
        "the live brand positioning when it is already accurate."
    ),
    "service_focus": (
        "Optimization objective: service or product focus. Lead with the primary offer on this page. "
        "Do not dilute the title with every service the business sells."
    ),
    "search_intent": (
        "Optimization objective: search-intent alignment. Match how searchers phrase the need for this page. "
        "Prefer observed Search Console queries that fit the page over volume alone."
    ),
    "ctr_clarity": (
        "Optimization objective: search-result clarity and CTR potential. Make the benefit obvious in the first words. "
        "Stay factual — no hype adjectives."
    ),
    "local_seo": (
        "Optimization objective: local SEO. Include the verified place when it belongs in the best keyword or live listing. "
        "Do not invent cities."
    ),
    "preserve_brand": (
        "Optimization objective: preserve current brand positioning. Keep the live listing's brand-first structure when it "
        "already names the business and core offers correctly. Only sharpen weak wording."
    ),
    "differentiation": (
        "Optimization objective: differentiation using verified evidence. Prefer a concrete, page-backed fact over a "
        "generic service list. Do not invent differentiators."
    ),
}

_GENERIC_OPENER = re.compile(
    r"^(business owners|buyers|customers|clients)\s+can\s+(explore|compare|discover|review|find)\b",
    re.I,
)
_SERVICE_COUNT = re.compile(r"\b(\d+)\s*(?:\+?\s*)?(core\s+)?services?\b", re.I)
_HYPE_WORDS = re.compile(r"\b(best[- ]in[- ]class|award[- ]winning|top[- ]rated|number one|#1|leading provider)\b", re.I)


def listing_fluff_problems(title: str, description: str, *, evidence: str = "") -> list[str]:
    """Reject invented counts, hype, and template openers that make listings worse than a solid live package."""
    issues: list[str] = []
    draft = f"{title or ''} {description or ''}"
    source = evidence or ""
    if _GENERIC_OPENER.search((description or "").strip()):
        issues.append(
            "Description opens with a generic template (“Business owners can explore…”). "
            "Write a natural sentence about this page's offer."
        )
    for match in _SERVICE_COUNT.finditer(draft):
        phrase = match.group(0)
        if phrase.lower() not in source.lower() and match.group(1) not in re.findall(r"\b\d+\b", source):
            issues.append(
                f"Do not invent “{phrase}”. Only use a service count when the page states that number."
            )
            break
    if _HYPE_WORDS.search(draft) and not _HYPE_WORDS.search(source):
        issues.append("Remove unverified hype (best, leading, top-rated, award-winning) unless the page states it.")
    return issues


_HARD_ISSUE_MARKERS = (
    "left the page topic",
    "different topic",
    "portfolio client",
    "unrelated client",
    "case-study title",
    "case study",
    "not a case study",
    "invent",
    "generic template",
    "contradicts the page",
    "is not on this page",
    "unverified hype",
    "misses the keyword",
    "left the business concept",
    "do not start the title with",
)


def is_hard_issue(issue: str) -> bool:
    """True when the issue is a factual/relevance failure — JEV must not override these."""
    text = (issue or "").lower()
    return any(marker in text for marker in _HARD_ISSUE_MARKERS)


def hard_issues_only(issues: list[str] | None) -> list[str]:
    return [item for item in (issues or []) if is_hard_issue(item)]


def objective_instruction(objective: str) -> str:
    key = (objective or "balanced").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "balanced_seo": "balanced",
        "balanced_seo_improvement": "balanced",
        "service": "service_focus",
        "product_focus": "service_focus",
        "intent": "search_intent",
        "ctr": "ctr_clarity",
        "clarity": "ctr_clarity",
        "local": "local_seo",
        "brand": "preserve_brand",
        "preserve": "preserve_brand",
        "differentiate": "differentiation",
    }
    key = aliases.get(key, key)
    return OBJECTIVES.get(key, OBJECTIVES["balanced"])



def _clean(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip().strip("\"'`"))
    if len(text) <= limit:
        return text.rstrip(" |–—-:")
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" .,;:-|–—")
    return cut or text[:limit].rstrip()


def _parse_meta(text: str) -> tuple[str, str, str]:
    title, desc, reason = "", "", ""
    for line in (text or "").splitlines():
        low = line.strip().lstrip("*-# ").replace("**", "")
        up = low.upper()
        if up.startswith("TITLE:"):
            title = low.split(":", 1)[-1].strip()
        elif up.startswith("DESCRIPTION:"):
            desc = low.split(":", 1)[-1].strip()
        elif up.startswith("REASON:"):
            reason = low.split(":", 1)[-1].strip()
    return _clean(title, 60), _clean(desc, 155), _clean(reason, 220)


def _parse_target(text: str) -> str:
    for line in (text or "").splitlines():
        low = line.strip().lstrip("*-# ").replace("**", "")
        if low.upper().startswith("TARGET:"):
            return _clean(low.split(":", 1)[-1], 80)
    return ""


def _tokens(value: str) -> set[str]:
    return {part for part in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(part) > 2}


def _overlap(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _issues(
    title: str,
    desc: str,
    current_title: str,
    page_bits: str,
    taken: list[str] | None = None,
    rivals: list[dict] | None = None,
) -> list[str]:
    issues = []
    for other in taken or []:
        if other and _overlap(title, other) >= 0.6:
            issues.append(f"Another page on this site already uses a title like “{other}”. Give this page its own angle.")
            break
    for rival in rivals or []:
        if rival.get("title") and _overlap(title, rival["title"]) >= 0.85:
            issues.append("This copies a competitor title. Meet them on the same keyword with a fact only this page can say.")
            break
    if len(title) < 42 or len(title) > 60:
        issues.append(f"Title is {len(title)} characters. Rewrite it to 50–60.")
    if len(desc) < 120 or len(desc) > 155:
        issues.append(f"Description is {len(desc)} characters. Rewrite it to 140–155.")
    blob = f"{title} {desc}".lower()
    for phrase in FILLER:
        if phrase in blob:
            issues.append(f"Remove the filler phrase “{phrase.strip()}”.")
    if current_title and _overlap(title, current_title) >= 0.9:
        issues.append("The title is the same as the current title. Keep the topic, and make the keyword clearer.")
    if title and desc and _overlap(title, desc) >= 0.7:
        issues.append("The description repeats the title. Add a fact and a next step instead.")
    page_words = _tokens(page_bits)
    title_words = _tokens(title)
    if page_words and title_words and len(title_words & page_words) < 2:
        issues.append("The title does not use the offer or place that is actually on this page.")
    if title.lower().startswith(("home", "welcome", "untitled")):
        issues.append("Do not start the title with Home or Welcome.")
    return issues[:6]


_TOPIC_STOP = {
    "the", "and", "for", "with", "your", "from", "this", "that", "page", "home",
    "our", "you", "are", "was", "has", "have", "its", "into", "about", "www",
}


def _content_tokens(value: str) -> set[str]:
    return {part for part in _tokens(value) if part not in _TOPIC_STOP}


_WEAK_TOPIC = {"home", "welcome", "untitled", "homepage", "index"}


def _meaningful_topic(value: str) -> str:
    text = _clean(str(value or "").split("|")[0], 80)
    words = _content_tokens(text)
    if not words or words <= _WEAK_TOPIC:
        return ""
    return text


def page_topic(scraped: dict | None) -> str:
    """The concept of this page. A bare H1 such as Home is skipped in favour of schema, Open Graph, or the title."""
    data = scraped or {}
    headings = data.get("headings")
    first_heading = ""
    if isinstance(headings, list) and headings:
        first_heading = str(headings[0])
    elif isinstance(headings, str):
        first_heading = headings
    for candidate in (data.get("h1"), data.get("schemaName"), data.get("ogTitle"), first_heading, data.get("title")):
        found = _meaningful_topic(str(candidate or ""))
        if found:
            return found
    return _clean(str(data.get("title") or data.get("h1") or ""), 80)


def _keyword_fit(query: str, topic: str, page_bits: str) -> float:
    words = _content_tokens(query)
    if not words:
        return 0.0
    topic_words = _content_tokens(topic)
    page_words = _content_tokens(page_bits)
    topic_hit = len(words & topic_words) / len(words) if topic_words else 0.0
    page_hit = len(words & page_words) / len(words) if page_words else 0.0
    return topic_hit * 2 + page_hit


def matching_queries(queries: list[str], topic: str, page_bits: str) -> list[str]:
    """Keep Search Console queries that belong to this page. Drop site-wide queries about a different topic."""
    scored: list[tuple[float, str]] = []
    topic_words = _content_tokens(topic)
    for query in queries:
        text = str(query).strip()
        score = _keyword_fit(text, topic, page_bits)
        shared = _content_tokens(text) & topic_words
        if score >= 0.5 or (shared and len(_content_tokens(text)) <= 5):
            scored.append((score, text))
    scored.sort(key=lambda item: item[0], reverse=True)
    kept: list[str] = []
    for _score, text in scored:
        if text.lower() not in {item.lower() for item in kept}:
            kept.append(text)
        if len(kept) >= 5:
            break
    return kept


def best_keyword(queries: list[str], topic: str) -> str:
    if queries:
        return queries[0]
    return topic


def _lead_phrase(title: str) -> str:
    head = str(title or "").split("|")[0].split(" - ")[0]
    return _clean(head, 80)


def _brand_tokens(brand: str, url: str = "") -> set[str]:
    host = ""
    match = re.search(r"https?://([^/]+)", url or "", re.I)
    if match:
        host = match.group(1).split(".")[0]
    return _content_tokens(brand) | _content_tokens(host)


def brand_only_keyword(keyword: str, brand: str, url: str = "") -> bool:
    """A search that is only the company name. It is not the homepage keyword."""
    words = _content_tokens(keyword)
    brand_words = _brand_tokens(brand, url)
    return bool(words) and bool(brand_words) and words <= brand_words


def coverage_keywords(
    *,
    offers: list[str],
    places: list[str],
    queries: list[str],
    brand: str,
    url: str = "",
) -> list[str]:
    """Best keyword first, then alternatives. A city is included only when that search already uses it."""
    offer_blob = " ".join(str(item) for item in offers if str(item).strip())
    place_words = set()
    for place in places:
        place_words |= _content_tokens(place)
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()

    def add(term: str, bonus: float) -> None:
        text = " ".join(str(term or "").split())[:80]
        key = text.lower()
        if not text or key in seen or brand_only_keyword(text, brand, url):
            return
        words = _content_tokens(text)
        if not words or words <= place_words:
            return
        fit = _keyword_fit(text, offer_blob, offer_blob) if offer_blob else 1.0
        if offer_blob and fit < 0.34 and not (words & _content_tokens(offer_blob)):
            return
        seen.add(key)
        ranked.append((fit + bonus, text))

    for offer in offers:
        add(str(offer), 2.0)
    for index, query in enumerate(queries):
        add(str(query), max(0.0, 1.2 - index * 0.15))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [text for _score, text in ranked[:4]]


def _rival_text(rival: dict) -> str:
    return " ".join(str(rival.get(key) or "") for key in ("title", "h1", "description"))


def rival_keyword_fit(rival: dict, keyword: str, offers: list[str] | None = None) -> int:
    """How many keyword (or offer) words appear in the competitor listing. Zero means unrelated."""
    target = _content_tokens(keyword)
    if len(target) < 2:
        target = _content_tokens(" ".join(str(item) for item in (offers or []) if str(item).strip()))
    if not target:
        return 0
    words = _content_tokens(_rival_text(rival))
    return len(words & target)


def rival_to_meet(rivals: list[dict], keyword: str, offers: list[str]) -> dict:
    """The listed competitor this listing has to be strong enough to stand next to.

    A Places store or an unrelated SERP result is never chosen just because it was first.
    The competitor must share the keyword (or the business offer). Named rivals only break ties.
    """
    pool = [rival for rival in rivals or [] if isinstance(rival, dict) and rival.get("title")]
    best, best_score = {}, 0
    for rival in pool:
        fit = rival_keyword_fit(rival, keyword, offers)
        if fit < 1:
            continue
        score = fit
        if "named" in str(rival.get("source") or "").lower():
            score += 1
        if "google results" in str(rival.get("source") or "").lower():
            score += 0.5
        if score > best_score:
            best, best_score = rival, score
    return best


def meets_competitor(title: str, rival: dict, keyword: str) -> bool:
    """True when the title can stand on the same search without copying the competitor."""
    if not rival or not rival.get("title"):
        return True
    if _overlap(title, rival["title"]) >= 0.85:
        return False
    need = _content_tokens(keyword)
    if len(need) >= 2 and len(_content_tokens(title) & need) < max(1, (len(need) + 1) // 2):
        return False
    return True


def conceptual_rivals(rivals: list[dict], offers: list[str], keyword: str = "") -> list[dict]:
    """Keep competitors that sell the same kind of thing. Drop unrelated search and Places results."""
    offer_blob = " ".join(str(item) for item in offers if str(item).strip())
    concept = offer_blob or keyword
    concept_words = _content_tokens(concept)
    kept = []
    for rival in rivals or []:
        if not isinstance(rival, dict) or not rival.get("title"):
            continue
        text = _rival_text(rival)
        words = _content_tokens(text)
        if not concept_words:
            # No offer yet: only keep SERP rows that already share the page keyword.
            if keyword and rival_keyword_fit(rival, keyword, offers) >= 1:
                kept.append(rival)
            continue
        shared = words & concept_words
        if shared or _keyword_fit(text, concept, concept) >= 0.34:
            kept.append(rival)
    return kept[:6]


def drops_homepage_promise(title: str, current: str, brand: str) -> bool:
    """True when a draft drops the offer and place the current title already states."""
    promise = _content_tokens(_lead_phrase(current)) - _brand_tokens(brand)
    if len(promise) < 2:
        return False
    lost = promise - _content_tokens(title)
    return len(lost) >= 2


def _named_services(description: str) -> list[str]:
    text = description or ""
    match = re.search(r"\bin\b(.+)$", text, re.I)
    chunk = match.group(1) if match else text
    services = []
    for part in re.split(r",|\band\b", chunk):
        label = _clean(re.sub(r"[.:;].*$", "", part), 32)
        words = _content_tokens(label)
        if not words or words <= {"selected", "work", "explore", "review", "including", "the", "cases"}:
            continue
        if label.lower() not in {item.lower() for item in services}:
            services.append(label)
    return services[:6]


def _proof_name(text: str, headings: str, brand: str, already: str) -> str:
    blob = f"{text or ''} {headings or ''}"
    used = _content_tokens(already) | _content_tokens(brand) | {"seo", "web", "ads", "google", "home", "selected", "client", "work"}
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", blob):
        name = match.group(1)
        if _content_tokens(name) & used:
            continue
        return name
    for match in re.finditer(r"\b([A-Z][A-Z0-9]{3,})\b", blob):
        name = match.group(1)
        if name.lower() in used:
            continue
        return name
    return ""


def _label_case(label: str) -> str:
    words = []
    for word in label.split():
        if word.isupper() or sum(1 for char in word if char.isupper()) >= 2:
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def _title_tail(services: list[str], lead: str) -> str:
    room = 60 - len(lead) - 3
    if room < 8:
        return ""
    chosen = []
    for service in services:
        if _content_tokens(service) <= _content_tokens(lead):
            continue
        trial = " & ".join([*chosen, _label_case(service)])
        if len(trial) <= room and len(chosen) < 2:
            chosen.append(_label_case(service))
    return " & ".join(chosen)


def _audience(description: str, lead: str) -> str:
    if re.search(r"business owners", description or "", re.I):
        place = (lead.split() or [""])[0]
        return f"{place} business owners".strip()
    return "Customers"


def stronger_listing(
    *,
    keyword: str,
    alternatives: list[str],
    offers: list[str],
    places: list[str],
    current_title: str,
    current_desc: str,
    headings: str,
    text: str,
    brand: str,
    page_kind: str = "",
) -> tuple[str, str, str]:
    """Build a keyword-led listing candidate for evaluation only.

    Never use this as an automatic inventing fallback when another candidate fails.
    The regenerate path must still compare the result to the published listing.
    Never invent service counts or template openers.
    """
    lead = _label_case((keyword or (offers[0] if offers else "") or _lead_phrase(current_title)).strip())
    extras = []
    for item in [*alternatives[1:], *offers]:
        label = " ".join(str(item).split())
        if not label or _content_tokens(label) <= _content_tokens(lead):
            continue
        # Never turn offer count into “N Core Services”.
        if _SERVICE_COUNT.search(label) or re.fullmatch(r"\d+", label):
            continue
        if label.lower() not in {row.lower() for row in extras}:
            extras.append(label)
    tail = _title_tail(extras, lead)
    if brand and brand.lower() not in lead.lower() and len(f"{lead} | {brand}") <= 60:
        title = _clean(f"{lead} | {tail}" if tail else f"{lead} | {brand}", 60)
    else:
        title = _clean(f"{lead} | {tail}" if tail else lead, 60)
    allow_proof = page_kind not in {"home", "service", "product", "category", "location"}
    proof = _proof_name(text, headings, brand, f"{current_title} {current_desc} {lead}") if allow_proof else ""
    cover = extras[:3]
    if cover:
        listed = ", ".join(cover[:-1]) + (f" and {cover[-1]}" if len(cover) > 1 else cover[0])
        if len(cover) == 1:
            listed = cover[0]
    else:
        listed = lead
    place = next((str(p).strip() for p in (places or []) if str(p).strip()), "")
    who = brand or "This team"
    if place and place.lower() not in listed.lower():
        sentence = f"{who} helps {place} businesses with {listed}."
    else:
        sentence = f"{who} offers {listed}."
    if proof:
        sentence = f"{sentence.rstrip('.')} — including work for {proof}."
    sentence = sentence.rstrip(".") + " Contact them to start."
    desc = _clean(sentence, 155)
    others = ", ".join(extras[:3]) or "the same offer"
    reason = f"Fallback listing led with “{lead}” and verified offers ({others}), without invented counts."
    return title, desc, reason


def listing_is_stronger(
    title: str,
    desc: str,
    current_title: str,
    current_desc: str,
    brand: str,
    *,
    keyword: str = "",
    places: list[str] | None = None,
    offers: list[str] | None = None,
    rival: dict | None = None,
) -> bool:
    """True when the draft beats the live listing — not a paste of the page or a near-copy of the current title."""
    _ = brand
    if not (title and desc):
        return False
    current = (current_title or "").strip()
    current_d = (current_desc or "").strip()
    if current and title.strip().lower() == current.lower() and desc.strip().lower() == current_d.lower():
        return False
    if current and _overlap(title, current) >= 0.75:
        return False
    if current_d and _overlap(desc, current_d) >= 0.85:
        return False
    concept = " ".join(str(item) for item in (offers or []) if str(item).strip()) or keyword
    concept_words = _content_tokens(concept)
    title_words = _content_tokens(title)
    if concept_words and not (title_words & concept_words):
        return False
    keyword_words = _content_tokens(keyword)
    if len(keyword_words) >= 2 and len(title_words & keyword_words) < max(1, (len(keyword_words) + 1) // 2):
        return False
    for place in places or []:
        name = str(place or "").strip()
        if not name or name.lower() in (keyword or "").lower():
            continue
        if title.split("|")[0].strip().lower().startswith(name.lower()):
            return False
    if rival and not meets_competitor(title, rival, keyword):
        return False
    if listing_fluff_problems(title, desc, evidence=f"{current} {current_d}"):
        return False
    # Live title already targets this keyword: the draft must add a new offer word or a new fact.
    current_words = _content_tokens(current)
    if keyword_words and len(keyword_words) >= 2 and len(current_words & keyword_words) >= max(1, (len(keyword_words) + 1) // 2):
        fresh = title_words - current_words - keyword_words
        alt_hit = False
        for offer in offers or []:
            offer_words = _content_tokens(offer)
            if offer_words and offer_words != keyword_words and (offer_words & title_words or offer_words & _content_tokens(desc)):
                alt_hit = True
                break
        if not fresh and not alt_hit:
            return False
    # Do not treat a thinner rewrite as stronger when the live title already names brand + offers.
    if current and brand and brand.lower() in current.lower():
        live_offers = sum(1 for offer in (offers or []) if _content_tokens(offer) & current_words)
        draft_offers = sum(1 for offer in (offers or []) if _content_tokens(offer) & title_words)
        if live_offers >= 2:
            if brand.lower() not in (title or "").lower():
                return False
            if draft_offers < live_offers:
                return False
            # Different words alone are not an advantage when the live listing already covers the offer set.
            if draft_offers <= live_offers and len(title_words) <= len(current_words):
                return False
    return True


def topic_issues(title: str, desc: str, topic: str, keyword: str) -> list[str]:
    """Reject a listing that left the page concept or dropped the keyword."""
    issues = []
    title_words = _content_tokens(title)
    topic_words = _content_tokens(topic)
    if topic_words and len(title_words & topic_words) < min(2, len(topic_words)):
        issues.append(f"The title left the page topic “{topic}”. The concept of this page must stay in the title.")
    keyword_words = _content_tokens(keyword)
    if len(keyword_words) >= 2 and len(title_words & keyword_words) < max(1, (len(keyword_words) + 1) // 2):
        issues.append(f"The title misses the keyword “{keyword}”. Use those words near the front.")
    desc_words = _content_tokens(desc)
    if topic_words and desc_words and not (desc_words & topic_words):
        issues.append(f"The description is about a different topic than “{topic}”.")
    return issues


def specificity_instruction(level: str) -> str:
    key = (level or "").strip().lower()
    if key in {"0", "exact", "exact-match", "exact-match suggestions"}:
        return (
            "Keyword specificity: exact. Use the offer and place already written on this page, "
            "and only a Search Console query this page can match directly. "
            "Do not add a related product, a broader category, or a nearby place."
        )
    if key in {"2", "broad", "broader", "broader discovery"}:
        return (
            "Keyword specificity: broad. You may use one nearby search angle this page can honestly support, "
            "such as a related size, season, or audience already implied by the copy. "
            "Do not invent a service the page does not offer."
        )
    return (
        "Keyword specificity: balanced. Use the page's real offer and the closest honest Search Console query. "
        "Stay specific. Do not drift into a different product."
    )


def _ask(user: str) -> tuple[str, str, str]:
    return chat_text(
        system=SYSTEM,
        user=user,
        kind="meta-seo",
        writing_type="Meta title & description",
        tokens=700,
        max_tokens=1200,
    )


def _analytics_block(analytics: dict | None) -> str:
    data = analytics or {}
    if not data.get("connected"):
        return "(Analytics is not connected. Do not invent sessions or bounce rate.)"
    lines = []
    for kpi in data.get("kpis") or []:
        if isinstance(kpi, (list, tuple)) and len(kpi) >= 2:
            lines.append(f"- {kpi[0]}: {kpi[1]}")
    for row in (data.get("channels") or [])[:6]:
        if isinstance(row, (list, tuple)) and row:
            sessions = row[1] if len(row) > 1 else "—"
            lines.append(f"- Channel {row[0]}: sessions {sessions}")
    landing = data.get("landing")
    if isinstance(landing, (list, tuple)) and landing:
        sessions = landing[1] if len(landing) > 1 else "—"
        bounce = landing[3] if len(landing) > 3 else "—"
        lines.append(f"- This landing page: sessions {sessions}, bounce {bounce}")
    else:
        lines.append("- This exact URL is not in the stored top landing pages.")
    return "\n".join(lines)


def _site_block(site: dict | None) -> str:
    data = site or {}
    business = data.get("business") or {}
    if not business and not data.get("siblings"):
        return "(The rest of the site was not read. Write from this page.)"
    lines = []
    if business.get("business"):
        lines.append(f"What the business is: {business['business']}")
    if business.get("brand"):
        lines.append(f"Brand name: {business['brand']}")
    if business.get("offers"):
        lines.append("Offers on the site: " + ", ".join(map(str, business["offers"][:10])))
    if business.get("audience"):
        lines.append(f"Who buys: {business['audience']}")
    if business.get("places"):
        lines.append("Places named on the site: " + ", ".join(map(str, business["places"][:8])))
    if business.get("proof"):
        lines.append("Facts the site states (usable proof): " + "; ".join(map(str, business["proof"][:8])))
    if business.get("voice"):
        lines.append(f"Brand voice: {business['voice']}")
    page = data.get("page") or {}
    if page.get("role") or page.get("target"):
        lines.append(f"This page's role: {page.get('role') or 'page'}. Search it should win: {page.get('target') or 'choose from the copy'}.")
    if page.get("angle"):
        lines.append(f"What sets this page apart from the other pages: {page['angle']}")
    siblings = data.get("siblings") or []
    if siblings:
        lines.append("Other pages on the site and the search each one covers (do not compete with them):")
        lines.extend(f"- {s.get('url')}: {s.get('target') or s.get('title') or ''}" for s in siblings[:24])
    return "\n".join(lines)


def _rival_block(rivals: list[dict]) -> str:
    if not rivals:
        return "(No competitor listings were available. Write the strongest honest listing from this page and the site.)"
    lines = []
    for rival in rivals[:6]:
        heads = " | ".join(rival.get("headings") or [])[:200]
        lines.append(
            f"- [{rival.get('source') or 'competitor'}] {rival.get('url') or ''}\n"
            f"  title: {rival.get('title') or '-'}\n"
            f"  description: {rival.get('description') or '-'}\n"
            f"  H1: {rival.get('h1') or '-'}  headings: {heads or '-'}"
        )
    return "\n".join(lines)


def write_title_description(
    *,
    url: str,
    profile: dict,
    gsc: dict | None = None,
    queries: list[str] | None = None,
    competitors: list[dict] | None = None,
    analytics: dict | None = None,
    specificity: str = "balanced",
    page: dict | None = None,
    site: dict | None = None,
    taken: list[str] | None = None,
    kind: str = "",
    research: dict | None = None,
    db=None,
    user=None,
    objective: str = "balanced",
    focus_topic: str = "",
    audience: str = "",
    primary_keyword: str = "",
    extra_instructions: str = "",
) -> dict:
    scraped = page if page and not page.get("error") and page.get("url") else scrape_page(url)
    kind = kind or page_quality.page_type(url, ((site or {}).get("page") or {}).get("role") or "", scraped)
    brand = str(((site or {}).get("business") or {}).get("brand") or profile.get("business") or "")
    headings = scraped.get("headings")
    if isinstance(headings, list):
        headings = " | ".join(headings)
    gsc = gsc or {}
    queries = [q for q in (queries or []) if str(q).strip()][:8]
    rivals = competitors or []
    page_only = " ".join(
        part for part in (
            scraped.get("h1"), headings, scraped.get("title"), scraped.get("description"),
            scraped.get("ogTitle"), scraped.get("ogDescription"), scraped.get("schemaName"),
            scraped.get("schemaDescription"), scraped.get("text"),
        ) if part
    )
    page_bits = page_only
    if site and site.get("business"):
        business = site["business"]
        page_bits += " " + " ".join(map(str, [*(business.get("offers") or []), *(business.get("places") or [])]))
    topic = page_topic(scraped)
    from app.listing_research import (
        best_keyword_from_research,
        page_anchors,
        research_lines,
        visibility_gap,
    )

    page_url = scraped.get("url") or url
    business = ((site or {}).get("business") or {})
    offers = [str(item).strip() for item in (business.get("offers") or []) if str(item).strip()]
    if not offers and profile.get("services"):
        offers = [part.strip() for part in str(profile.get("services")).split(",") if part.strip()]
    places = [str(item).strip() for item in (business.get("places") or []) if str(item).strip()]
    if not places and (profile.get("areas") or profile.get("locations")):
        places = [part.strip() for part in str(profile.get("areas") or profile.get("locations")).split(",") if part.strip()]
    from app.listing_research import keyword_fits_page

    research_best, research_alts = best_keyword_from_research(
        research,
        url=page_url,
        topic=topic or " ".join(offers[:2]),
        offers=offers,
        queries=queries,
        page_text=page_only,
        places=places,
    )
    # Drop stored research terms that fail page relevance (volume alone is never enough).
    if research_best and not keyword_fits_page(
        research_best, topic=topic or "", page_text=page_only, offers=offers, places=places,
    ):
        research_best = ""
    research_alts = [
        term for term in research_alts
        if keyword_fits_page(term, topic=topic or "", page_text=page_only, offers=offers, places=places)
    ]
    # Match research + GSC against the page. Never force research_best into matched when it fails.
    matched = matching_queries(
        [*( [research_best] if research_best else [] ), *research_alts, *queries],
        topic or " ".join(offers),
        page_only,
    )
    if research_best and research_best.lower() not in {item.lower() for item in matched}:
        research_best = ""  # not page-relevant enough for matching_queries
    options = coverage_keywords(
        offers=offers, places=places, queries=[*matched, *research_alts, *queries], brand=brand, url=page_url,
    )
    keyword_evidence_insufficient = False
    # Selection order: page-matched evidence → offer coverage → page topic. Never invent research.
    if matched:
        keyword = matched[0]
    elif options:
        keyword = options[0]
        keyword_evidence_insufficient = not bool(queries or research_alts)
    else:
        keyword = topic or (offers[0] if offers else "")
        keyword_evidence_insufficient = True
    page_body_early = str(scraped.get("text") or "")
    page_heads_early = str(headings or "")
    portfolio_clients_early = page_quality.portfolio_client_names(
        page_body_early, page_heads_early, brand=brand, places=places, offers=offers,
    )
    user_keyword = " ".join(str(primary_keyword or "").split())[:80]
    if user_keyword:
        client_bleed = any(name.lower() in user_keyword.lower() for name in portfolio_clients_early)
        fits = keyword_fits_page(
            user_keyword,
            topic=topic or focus_topic or "",
            # Offer/topic only — full page text can contain portfolio client names.
            page_text=f"{topic} {focus_topic} {' '.join(offers)}",
            offers=offers,
            places=places,
        )
        if client_bleed and kind in {"home", "service", "product", "category", "location", ""}:
            user_keyword = ""
        elif fits:
            keyword = user_keyword
            if user_keyword.lower() not in {item.lower() for item in options}:
                options = [user_keyword, *options][:4]
        else:
            user_keyword = ""  # rejected — does not fit this page
    if focus_topic.strip() and not topic:
        topic = focus_topic.strip()[:120]
    objective_key = (objective or "balanced").strip().lower().replace("-", "_").replace(" ", "_") or "balanced"
    place_words = set()
    for place in places:
        place_words |= _content_tokens(place)
    if not topic or _content_tokens(topic) <= place_words or brand_only_keyword(topic, brand, page_url):
        topic = offers[0] if offers else keyword
    rivals = conceptual_rivals(rivals, offers, keyword=keyword)
    meet = rival_to_meet(rivals, keyword, offers)
    query_block = "\n".join(f"- {q}" for q in (options or matched or research_alts)) or "(No Search Console query matches this business yet. Use the business offer as the keyword.)"
    schema_types = ", ".join(scraped.get("schemaTypes") or []) or "(none found)"
    current_listing = (
        f"Current meta title: {scraped.get('title') or '(none)'}\n"
        f"Current meta description: {scraped.get('description') or '(none)'}\n"
        f"Current Open Graph title: {scraped.get('ogTitle') or '(none)'}\n"
        f"Current Open Graph description: {scraped.get('ogDescription') or '(none)'}\n"
        f"Current JSON-LD type: {schema_types}\n"
        f"Current JSON-LD name: {scraped.get('schemaName') or '(none)'}\n"
        f"Current JSON-LD description: {scraped.get('schemaDescription') or '(none)'}\n"
        f"Canonical: {scraped.get('canonical') or '(same as this page, or not set)'}\n"
        f"Navigation labels: {', '.join(scraped.get('nav') or []) or '(none read)'}\n"
        f"FAQ questions on the page: {', '.join(scraped.get('faqs') or []) or '(none)'}\n"
        f"Videos on the page: {', '.join(scraped.get('videos') or []) or '(none)'}\n"
        f"Reviews in schema: {scraped.get('reviewRating') or '(no rating)'} from {scraped.get('reviewCount') or '(no count)'} reviews"
    )

    empty = {
        "title": _clean(scraped.get("title") or "", 60),
        "metaDescription": _clean(scraped.get("description") or "", 155),
        "reason": "OpenAI is not configured, so the live title was kept.",
        "model": "",
        "role": "none",
        "draftBody": "",
        "scraped": {**scraped, "headings": headings or ""},
        "competitors": rivals,
        "meetCompetitor": {k: meet.get(k) for k in ("title", "url", "description", "source")} if meet else {},
        "beatsLive": False,
        "openIssues": ["OpenAI is not configured, so the live title was kept."],
        "queries": queries,
        "analytics": analytics or {},
        "target": "",
    }

    def seo_content_draft() -> dict:
        from app import dfs_meta

        return dfs_meta.draft_from_research(
            db,
            user,
            keyword=keyword or topic or "",
            topic=topic or "",
            offers=offers,
            current_title=str(scraped.get("title") or ""),
            current_desc=str(scraped.get("description") or ""),
            page_text=str(scraped.get("text") or ""),
            headings=str(headings or ""),
            rival_title=str(meet.get("title") or ""),
            anchors=page_anchors(research, page_url),
            visibility_gap=visibility_gap(research),
        )

    if not openai_configured():
        # DataForSEO-only path: same validation gates as OpenAI. Never invent a replacement.
        from app.cms_connectors import build_listing_json_ld
        from app.jev import score_listing
        from app.page_scrape import suggest_images

        live_title = str(scraped.get("title") or "")
        live_desc = str(scraped.get("description") or "")
        evidence_text = " ".join(
            str(part)
            for part in (
                scraped.get("title"), scraped.get("description"), scraped.get("h1"),
                headings, scraped.get("text"),
            )
            if part
        )
        page_body = str(scraped.get("text") or "")
        page_heads = str(headings or "")

        def dfs_hard_checks(t: str, d: str) -> list[str]:
            found = _issues(t, d, live_title, page_bits, taken or [], rivals)
            found.extend(topic_issues(t, d, topic, keyword))
            found.extend(listing_fluff_problems(t, d, evidence=evidence_text))
            claims = page_quality.validate_claims(t, d, evidence=evidence_text, places=places)
            for claim in claims["claims"]:
                if claim["status"] == "contradicted":
                    found.append(
                        f"“{claim['claim']}” contradicts the page, which says {claim.get('page')}. "
                        "Use the page's figure or drop it."
                    )
                elif claim["status"] == "unverified" and claim["kind"] in {"number", "promise"}:
                    found.append(f"“{claim['claim']}” is not on this page. Remove it unless the page says it.")
            if kind == "home":
                found.extend(
                    page_quality.homepage_problems(
                        t, d, brand=brand, places=places, offers=offers,
                        page_text=page_body, headings=page_heads,
                    )
                )
            elif kind in {"service", "product", "category", "location"}:
                found.extend(
                    page_quality.service_page_problems(
                        t, d, topic=topic or keyword or "", page_text=page_body, headings=page_heads,
                        brand=brand, places=places, offers=offers,
                    )
                )
            elif kind == "case_study":
                found.extend(page_quality.case_study_problems(t, d, scraped, brand))
            elif kind in {"portfolio", "other"}:
                clients = page_quality.portfolio_client_names(
                    page_body, page_heads, brand=brand, places=places, offers=offers,
                )
                for name in clients:
                    if name.lower() in f"{t} {d}".lower() and name.lower() not in (topic or "").lower():
                        found.append(
                            f"Do not name the portfolio client “{name}” as the archive title. "
                            "Keep the listing about the portfolio as a whole."
                        )
                        break
            if live_title.strip() and offers and not (_content_tokens(t) & _content_tokens(" ".join(offers))):
                found.append("The title left the business concept. Use the best keyword from the offers, not the current title.")
            for place in places:
                name = str(place or "").strip()
                if name and name.lower() not in (keyword or "").lower() and t.split("|")[0].strip().lower().startswith(name.lower()):
                    found.append(f"Do not start the title with {name}. The best keyword does not lead with that city.")
                    break
            return [item for item in found if is_hard_issue(item)][:8]

        seeded = seo_content_draft()
        target_kw = keyword or topic or ""
        keep_reason = (
            "No-change recommendation: OpenAI is not configured and Searchify SEO content generation "
            "did not produce a validated, demonstrably stronger listing than the published title."
        )
        if keyword_evidence_insufficient:
            keep_reason = (
                "No-change recommendation: keyword evidence is insufficient for this page, and no validated "
                "alternative beat the published listing."
            )

        recommend_keep = True
        beats = False
        open_issues: list[str] = []
        seo_title = _clean(live_title, 60)
        seo_desc = _clean(live_desc, 155)
        reason = empty["reason"] if not (live_title or live_desc) else keep_reason
        model = ""
        role = "none"
        provider_failed = False

        if seeded.get("title") and seeded.get("description"):
            cand_title = _clean(seeded["title"], 60)
            cand_desc = _clean(seeded["description"], 155)
            hard = dfs_hard_checks(cand_title, cand_desc)
            stronger = bool(live_title.strip()) and listing_is_stronger(
                cand_title, cand_desc, live_title, live_desc, brand,
                keyword=keyword, places=places, offers=offers, rival=meet,
            )
            if hard:
                # Failed hard checks — keep published. Do not invent a stronger_listing replacement.
                open_issues = []  # checks ran; published is the recommendation
                reason = (
                    "No-change recommendation: the Searchify SEO content draft failed factual or relevance checks. "
                    "Kept the published listing rather than inventing a replacement."
                )
                recommend_keep = True
                beats = False
                model, role = "searchify-seo-content", "content"
            elif not live_title.strip() or stronger:
                seo_title, seo_desc = cand_title, cand_desc
                recommend_keep = False
                beats = bool(live_title.strip()) and stronger
                open_issues = hard
                reason = (
                    f"Drafted with Searchify SEO content generation on “{target_kw}”. "
                    "Compare it to the live title before approving."
                )
                model, role = "searchify-seo-content", "content"
            else:
                reason = keep_reason
                recommend_keep = True
                beats = False
                open_issues = []
                model, role = "searchify-seo-content", "content"
        else:
            # Provider missing/failed — do not claim a successful regenerated draft.
            from app import research as research_mod

            provider_failed = research_mod.configured()
            if provider_failed:
                reason = (
                    "No-change recommendation: Searchify SEO content generation was unavailable or returned "
                    "nothing usable. Kept the published listing."
                )
                open_issues = ["Searchify SEO content generation failed or returned an empty draft."]
            else:
                reason = empty["reason"]
                open_issues = list(empty["openIssues"])
                provider_failed = False

        images = suggest_images(scraped.get("images"), topic, page_url)
        share = str(scraped.get("ogImage") or "").strip() or next((row["src"] for row in images if row.get("src")), "")
        package = build_listing_json_ld(
            meta_title=seo_title,
            meta_description=seo_desc,
            page_url=page_url,
            site_name=brand,
            schema_types=scraped.get("schemaTypes") or [],
            share_image=share,
        ) if seo_title or seo_desc else {}
        provider_error = bool(
            provider_failed and not (seeded.get("title") and seeded.get("description"))
        )
        jev = score_listing(
            title=seo_title,
            description=seo_desc,
            keyword=target_kw,
            live_title=live_title,
            live_description=live_desc,
            beats_live=beats,
            meet_competitor=str(meet.get("title") or ""),
            open_issues=open_issues if (not recommend_keep or provider_error) else [],
            hard_fail=provider_error or bool(hard_issues_only(open_issues) and not recommend_keep),
        )
        schema_note = package.pop("_schemaNote", "") if isinstance(package, dict) else ""
        return {
            **empty,
            "pageType": kind,
            "title": seo_title,
            "metaDescription": seo_desc,
            "openGraphTitle": (
                _clean(str(scraped.get("ogTitle") or live_title or seo_title), 60)
                if recommend_keep
                else seo_title
            ),
            "openGraphDescription": (
                _clean(str(scraped.get("ogDescription") or live_desc or seo_desc), 155)
                if recommend_keep
                else seo_desc
            ),
            "shareImage": share,
            "images": images,
            "jsonLd": package,
            "schemaTypesPreserved": list(scraped.get("schemaTypes") or []),
            "reason": reason,
            "model": model,
            "role": role,
            "target": target_kw,
            "beatsLive": beats,
            "recommendKeep": recommend_keep,
            "openIssues": open_issues,
            "jev": jev,
            "objective": objective_key,
            "analysis": {
                "sources": [
                    part for part in (
                        "Live page (title, description, Open Graph, JSON-LD)",
                        "Searchify SEO content generation" if model == "searchify-seo-content" else "",
                        "Searchify SEO keyword and competitor research" if research else "",
                        f"Competitor to meet: {meet.get('title')}" if meet.get("title") else "No same-keyword competitor stored",
                        "JEV draft score",
                    ) if part
                ],
                "keyword": target_kw,
                "keywordSource": (
                    "operator" if user_keyword and keyword == user_keyword
                    else "dataforseo_ranked" if research_best and keyword == research_best
                    else "coverage_or_gsc"
                ),
                "keywordEvidenceInsufficient": keyword_evidence_insufficient,
                "beatsLive": beats,
                "recommendKeep": recommend_keep,
                "currentAssessment": (
                    "The published listing already names the business and core offers clearly."
                    if recommend_keep
                    else "A validated content draft was stronger than the published listing."
                ),
                "improvement": (
                    "None — keep the published listing."
                    if recommend_keep
                    else "Clearer keyword-led title from Searchify SEO content generation."
                ),
                "tradeOff": (
                    "No change — avoids replacing accurate brand positioning with an unvalidated rewrite."
                    if recommend_keep
                    else "Brand may move later in the title so the offer leads."
                ),
                "meetCompetitor": str(meet.get("title") or ""),
                "contentWriter": model == "searchify-seo-content",
                "providerFailed": bool(provider_failed and not (seeded.get("title") and seeded.get("description"))),
                "schemaNote": schema_note,
                "objective": objective_key,
            },
        }

    short_note = ""
    if len(str(scraped.get("text") or "").split()) < 120:
        short_note = (
            "The body copy on this page is short. Write from the whole-site understanding, this page's title, heading, Open Graph, and schema. "
            "Do not invent a service that is not in the site offers.\n"
        )
    page_brief = (
        f"PAGE BRIEF (evidence from the connected URL — do not invent beyond this):\n"
        f"- URL: {scraped.get('url') or url}\n"
        f"- Page type: {kind or 'page'}\n"
        f"- Page topic: {topic or '(derive from H1 and offers on this page)'}\n"
        f"- Search intent: {'commercial / transactional for service or product pages' if kind in {'service', 'product', 'category', 'location'} else 'navigational / brand for homepage' if kind == 'home' else 'informational or commercial as the copy supports'}\n"
        f"- Verified brand: {brand or '(none found)'}\n"
        f"- Offers confirmed on site: {', '.join(offers[:8]) or '(none)'}\n"
        f"- Places named: {', '.join(places[:6]) or '(none)'}\n"
        f"- Existing title: {scraped.get('title') or '(none)'}\n"
        f"- Existing description: {scraped.get('description') or '(none)'}\n"
        f"- Keyword evidence: {'insufficient — use page topic, do not invent research' if keyword_evidence_insufficient else (keyword or topic or 'page topic')}\n"
        f"- Canonical: {scraped.get('canonical') or '(same as page or unset)'}\n"
        f"- Schema types present: {schema_types}\n"
    )
    brief = (
        f"Business: {profile.get('business') or ''}\n"
        f"Services: {profile.get('services') or ''}\n"
        f"Places: {profile.get('areas') or profile.get('locations') or ''}\n"
        f"Owner's 90-day aim: {profile.get('goal') or '(not given)'}\n"
        f"Voice: {profile.get('voice') or 'specific and concrete'}\n"
        f"Only claim what is on the page or the site. Allowed notes: {profile.get('claims') or 'visible page facts only'}\n"
        f"Do not say: {profile.get('restrictions') or profile.get('rules') or 'invented awards, prices, or results'}\n\n"
        f"{page_brief}\n"
        f"1. BUSINESS CONCEPT. Decide from this before you look at the current title:\n{_site_block(site)}\n"
        f"PAGE TOPIC inside that business: {topic or '(use the main offer)'}\n"
        f"2. KEYWORD COVERAGE. Best keyword first. Use the first one near the front of the title. "
        f"Use a different one in the description so the listing covers more than one search. "
        f"A city leads the title only when the best keyword itself contains that city:\n{query_block}\n"
        f"BEST KEYWORD: {keyword or topic or '(use the business offer)'}\n\n"
        f"3. COMPETITOR TO MEET. Only use a competitor that sells the same kind of thing on this keyword. "
        f"Never meet a kitchen store, cookware shop, or unrelated Places listing for a web or marketing page. "
        f"When a hard competitor is listed, the title must stand on the same search with the best keyword and a fact only this business can say. Do not copy their title:\n{meet.get('title') or '(none that share this keyword — write from the business offer and the live page)'}\n"
        f"Other competitors that sell the same kind of service:\n{_rival_block(rivals)}\n\n"
        f"4. SEARCH CONSOLE AND RESEARCH, already matched to this business:\n{research_lines(research, page_url, topic, offers)}\n"
        f"This URL in Search Console: clicks={gsc.get('clicks')} impressions={gsc.get('impressions')} position={gsc.get('position')}\n\n"
        f"5. CURRENT LISTING. This is what is live. Beat it. Do not copy it, and do not start with its city unless the best keyword contains that city:\n{current_listing}\n"
        f"Page: {scraped.get('url') or url}\n"
        f"H1: {scraped.get('h1') or '(none)'}\n"
        f"Section headings: {headings or '(none)'}\n"
        f"Page copy:\n{(scraped.get('text') or scraped.get('error') or '(could not read the page)')[:3200]}\n"
        f"{short_note}\n"
        f"Analytics for the property, last stored window. Use a figure only if it is written here:\n{_analytics_block(analytics)}\n\n"
        f"{specificity_instruction(specificity)}\n"
        f"{objective_instruction(objective_key)}\n"
    )
    if audience.strip():
        brief += f"Target audience for this rewrite (use only if it fits the page): {audience.strip()[:120]}\n"
    if focus_topic.strip():
        brief += f"Primary service/product/topic emphasis: {focus_topic.strip()[:120]}\n"
    if user_keyword:
        brief += f"Operator-selected primary keyword (already validated against this page): {user_keyword}\n"
    if extra_instructions.strip():
        brief += f"Additional operator instructions (must remain factual): {extra_instructions.strip()[:400]}\n"
    page_body = str(scraped.get("text") or "")
    page_heads = str(headings or "")
    portfolio_clients = page_quality.portfolio_client_names(
        page_body, page_heads, brand=brand, places=places, offers=offers,
    )
    if kind == "home":
        brief += (
            "\nThis is the HOMEPAGE. Describe the business offer and place — not a portfolio piece. "
            "The business concept chooses the keyword, not the current title. "
            "Lead with the best keyword. Put one alternative keyword in the description. "
            "Start with a city only when that best keyword contains the city. "
            "Do not write a case-study title. Do not name any client project shown in selected work. "
            "Do not invent a service count such as “5 Core Services”.\n"
        )
        if portfolio_clients:
            brief += (
                "Portfolio clients visible on this homepage (do NOT use these names in the title or description): "
                + ", ".join(portfolio_clients[:8])
                + ".\n"
            )
    if kind in {"service", "product", "category", "location"}:
        brief += (
            f"\nThis is a {kind.upper()} page. Stay on this page's subject ({topic or keyword or 'the main offer'}). "
            "Do not import a case study, portfolio client, or a different service from elsewhere on the site.\n"
        )
        if portfolio_clients:
            brief += (
                "Unrelated client names on or near this page (do NOT use unless this page is about them): "
                + ", ".join(portfolio_clients[:8])
                + ".\n"
            )
    if kind == "case_study":
        brief += (
            "\nThis page is a CASE STUDY about work the site owner did for a client. "
            "The client's name, city and industry belong to the client, not to the site owner. "
            "Write the listing as a case study by the site owner (for example \"Case study: …\" or \"How we helped …\"). "
            "Never present the client's location or business as the site owner's.\n"
        )
    if taken:
        brief += "\nTitles already written for other pages on this site (yours must differ):\n" + "\n".join(f"- {t}" for t in taken[:20]) + "\n"
    current = scraped.get("title") or ""
    live_desc = str(scraped.get("description") or "")
    evidence_text = " ".join(
        str(part) for part in (scraped.get("title"), scraped.get("description"), scraped.get("h1"), headings, scraped.get("text")) if part
    )

    def hard_checks(t: str, d: str) -> list[str]:
        """Factual and relevance gates shared by every candidate source."""
        found = _issues(t, d, current, page_bits, taken, rivals)
        found.extend(topic_issues(t, d, topic, keyword))
        found.extend(listing_fluff_problems(t, d, evidence=evidence_text))
        claims = page_quality.validate_claims(t, d, evidence=evidence_text, places=places)
        for claim in claims["claims"]:
            if claim["status"] == "contradicted":
                found.append(f"“{claim['claim']}” contradicts the page, which says {claim.get('page')}. Use the page's figure or drop it.")
            elif claim["status"] == "unverified" and claim["kind"] in {"number", "promise"}:
                found.append(f"“{claim['claim']}” is not on this page. Remove it unless the page says it.")
        if kind == "home":
            found.extend(
                page_quality.homepage_problems(
                    t, d, brand=brand, places=places, offers=offers, page_text=page_body, headings=page_heads,
                )
            )
        elif kind in {"service", "product", "category", "location"}:
            found.extend(
                page_quality.service_page_problems(
                    t, d, topic=topic or keyword or "", page_text=page_body, headings=page_heads,
                    brand=brand, places=places, offers=offers,
                )
            )
        elif kind == "case_study":
            found.extend(page_quality.case_study_problems(t, d, scraped, brand))
        elif kind in {"portfolio", "other"}:
            clients = page_quality.portfolio_client_names(
                page_body, page_heads, brand=brand, places=places, offers=offers,
            )
            for name in clients:
                if name.lower() in f"{t} {d}".lower() and name.lower() not in (topic or "").lower():
                    found.append(
                        f"Do not name the portfolio client “{name}” as the archive title. "
                        "Keep the listing about the portfolio as a whole."
                    )
                    break
        if current.strip() and offers and not (_content_tokens(t) & _content_tokens(" ".join(offers))):
            found.append("The title left the business concept. Use the best keyword from the offers, not the current title.")
        for place in places:
            name = str(place or "").strip()
            if name and name.lower() not in (keyword or "").lower() and t.split("|")[0].strip().lower().startswith(name.lower()):
                found.append(f"Do not start the title with {name}. The best keyword does not lead with that city.")
                break
        return [item for item in found if is_hard_issue(item)][:8]

    def checks(t: str, d: str) -> list[str]:
        found = list(hard_checks(t, d))
        if current.strip() and not listing_is_stronger(
            t, d, current, live_desc, brand, keyword=keyword, places=places, offers=offers, rival=meet,
        ):
            found.insert(0, (
                "The draft is not stronger than the live title. Lead with the best keyword, keep a fact only this page can say, "
                "and do not paste the current listing or the page headings."
            ))
        if meet.get("title") and not meets_competitor(t, meet, keyword):
            found.insert(0, (
                f"The title does not meet “{meet.get('title')}”. Put “{keyword}” near the front and add a fact only this page can say."
            ))
        return found[:8]

    seo_seed = seo_content_draft()
    seed_note = ""
    if seo_seed.get("title") and seo_seed.get("description"):
        seed_note = (
            "\nSearchify SEO content generation offered this draft to beat when it is stronger and on-keyword:\n"
            f"TITLE: {seo_seed['title']}\nDESCRIPTION: {seo_seed['description']}\n"
            "You may improve it. Keep the best keyword near the front. Do not invent facts.\n"
        )
    text, model, role = _ask(brief + seed_note + "\nWrite the listing for this page only.")
    title, desc, reason = _parse_meta(text)
    issues = checks(title, desc)
    if issues:
        retry_text, retry_model, retry_role = _ask(
            brief
            + seed_note
            + "\nThe first draft failed these checks:\n- "
            + "\n- ".join(issues)
            + f"\n\nRejected title: {title or '(empty)'}\nRejected description: {desc or '(empty)'}\n"
            + "Write a new listing that fixes every check. Keep the page topic and put the best keyword near the front of the title."
        )
        retry_title, retry_desc, retry_reason = _parse_meta(retry_text)
        retry_issues = checks(retry_title, retry_desc)
        if len(retry_issues) <= len(issues) and retry_title and retry_desc:
            title, desc, reason = retry_title, retry_desc, retry_reason
            text, model, role = retry_text, retry_model, retry_role
            issues = retry_issues

    def score_candidate(cand_title: str, cand_desc: str) -> tuple[int, int, int]:
        """Lower is better: open issues, not-stronger flag, distance from ideal length."""
        open_count = len(checks(cand_title, cand_desc))
        weak = 0 if listing_is_stronger(
            cand_title, cand_desc, current, live_desc, brand, keyword=keyword, places=places, offers=offers, rival=meet,
        ) else 1
        length_penalty = abs(len(cand_title) - 55) + abs(len(cand_desc) - 148)
        return (open_count, weak, length_penalty)

    if seo_seed.get("title") and seo_seed.get("description"):
        seo_title = _clean(seo_seed["title"], 60)
        seo_desc = _clean(seo_seed["description"], 155)
        seo_bleed = any(
            "portfolio client" in issue.lower()
            or "unrelated client" in issue.lower()
            or "case-study title" in issue.lower()
            or "not a case study" in issue.lower()
            for issue in checks(seo_title, seo_desc)
        )
        if not seo_bleed and score_candidate(seo_title, seo_desc) < score_candidate(title, desc):
            title, desc = seo_title, seo_desc
            reason = (
                f"Chose the Searchify SEO content draft on “{keyword or topic}” because it beat the first pass "
                "on keyword fit and strength versus the live listing."
            )
            model, role = "searchify-seo-content", "content"
            text = f"TITLE: {title}\nDESCRIPTION: {desc}\nREASON: {reason}"

    # Never invent a stronger_listing replacement when a candidate fails — keep published instead.
    hard_fail = hard_checks(title, desc) if title and desc else ["The writer returned no title."]
    if hard_fail and kind in {"home", "service", "product", "category", "location", "case_study", "portfolio", "other", ""}:
        import logging

        logging.getLogger(__name__).info(
            "meta_writer rejected hard-fail draft for %s (%s): %s",
            kind,
            page_url,
            "; ".join(hard_fail[:3]),
        )
        if current.strip() and live_desc.strip():
            title, desc = _clean(current, 60), _clean(live_desc, 155)
            reason = (
                "No-change recommendation: the draft failed factual or relevance checks. "
                "Kept the published listing rather than inventing a replacement."
            )
    recommend_keep = False
    if current.strip() and live_desc.strip():
        same = title.strip().lower() == current.strip().lower() and desc.strip().lower() == live_desc.strip().lower()
        stronger = listing_is_stronger(
            title, desc, current, live_desc, brand, keyword=keyword, places=places, offers=offers, rival=meet,
        )
        fluff = listing_fluff_problems(title, desc, evidence=evidence_text)
        remaining_hard = hard_checks(title, desc) if title and desc else []
        if same or (not stronger) or fluff or remaining_hard:
            title, desc = _clean(current, 60), _clean(live_desc, 155)
            recommend_keep = True
            if keyword_evidence_insufficient:
                reason = (
                    "No-change recommendation: keyword evidence is insufficient for this page. "
                    "Kept the published listing rather than inventing research-led copy."
                )
            else:
                reason = (
                    "No-change recommendation: the published title and description already represent this page accurately. "
                    "Regeneration did not produce a validated, demonstrably stronger listing."
                )
    if not reason:
        reason = "Written from the business concept, the best keyword, and the competitor this page has to meet."
    if keyword and "best keyword" not in reason.lower() and f"“{keyword}”" not in reason and not recommend_keep:
        reason = f"Best keyword “{keyword}”. {reason}".strip()
    if meet.get("title") and keyword and not recommend_keep:
        meeting = f"Written to meet {meet['title'][:90]} on “{keyword}”."
        if meeting.lower() not in reason.lower():
            reason = f"{meeting} {reason}".strip()
    if seo_seed and "searchify seo" not in reason.lower() and model != "searchify-seo-content" and not recommend_keep:
        reason = f"{reason} Compared with a Searchify SEO content draft.".strip()
    validation = page_quality.validate_claims(title, desc, evidence=evidence_text, places=places)
    from app.cms_connectors import build_listing_json_ld
    from app.page_scrape import suggest_images

    images = suggest_images(scraped.get("images"), topic, page_url)
    share = str(scraped.get("ogImage") or "").strip() or next((row["src"] for row in images if row.get("src")), "")
    package = build_listing_json_ld(
        meta_title=title,
        meta_description=desc,
        page_url=page_url,
        site_name=brand,
        schema_types=scraped.get("schemaTypes") or [],
        share_image=share,
    ) if title or desc else {}
    open_issues = [] if recommend_keep else (checks(title, desc) if title else ["The writer returned no title."])
    beats_live = (
        False
        if recommend_keep
        else bool(title and desc) and listing_is_stronger(
            title, desc, current, live_desc, brand, keyword=keyword, places=places, offers=offers, rival=meet,
        )
    )
    target_kw = _parse_target(text) or keyword or ((site or {}).get("page") or {}).get("target") or ""
    # Preserve distinct social copy when recommending keep; otherwise align OG with the proposed listing.
    if recommend_keep:
        og_title = _clean(str(scraped.get("ogTitle") or current or title), 60)
        og_desc = _clean(str(scraped.get("ogDescription") or live_desc or desc), 155)
    else:
        og_title, og_desc = title, desc
    from app.jev import score_listing

    jev = score_listing(
        title=title,
        description=desc,
        keyword=target_kw or keyword,
        live_title=current,
        live_description=live_desc,
        beats_live=beats_live,
        meet_competitor=str(meet.get("title") or ""),
        open_issues=open_issues,
        hard_fail=bool(hard_issues_only(open_issues)),
    )
    current_assessment = (
        "The published listing already names the business and core offers clearly."
        if recommend_keep
        else (
            "The published listing can be clearer on keyword-first phrasing or a missing verified offer."
            if beats_live
            else "The published listing remains competitive; proposed changes were not clearly stronger."
        )
    )
    analysis = {
        "sources": [
            part for part in (
                "Live page (title, description, Open Graph, JSON-LD)",
                "Business offers and places",
                "Search Console queries and page clicks" if gsc else "",
                "Searchify SEO keyword and competitor research" if research else "",
                "Searchify SEO content generation" if (seo_seed and seo_seed.get("title")) or model == "searchify-seo-content" else "",
                f"Competitor to meet: {meet.get('title')}" if meet.get("title") else "No same-keyword competitor stored",
                "Analytics landing facts (not a ranking source)" if (analytics or {}).get("connected") else "",
                "JEV draft score",
            ) if part
        ],
        "keyword": target_kw or keyword or "",
        "keywordSource": (
            "operator" if user_keyword and keyword == user_keyword
            else "dataforseo_ranked" if research_best and keyword == research_best
            else "coverage_or_gsc"
        ),
        "keywordEvidenceInsufficient": keyword_evidence_insufficient,
        "objective": objective_key,
        "objectiveLabel": objective_instruction(objective_key).split(":", 1)[-1].strip()[:160],
        "focusTopic": (focus_topic or "").strip()[:120],
        "audience": (audience or "").strip()[:120],
        "beatsLive": beats_live,
        "recommendKeep": recommend_keep,
        "pageType": kind,
        "pageUrl": page_url,
        "measured": False,
        "inferred": True,
        "dataKind": "listing_recommendation",
        "provider": model or "openai",
        "generatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "currentAssessment": current_assessment,
        "improvement": (
            "None — keep the published listing."
            if recommend_keep
            else "Clearer keyword-led title and a description that states the verified offer without template filler."
        ),
        "tradeOff": (
            "No change — avoids replacing accurate brand positioning with a thinner rewrite."
            if recommend_keep
            else "Brand may move later in the title so the offer leads."
        ),
        "meetCompetitor": str(meet.get("title") or ""),
        "contentWriter": model == "searchify-seo-content" or bool(seo_seed and seo_seed.get("title")),
        "gscScope": "page_or_property" if gsc else "missing",
        "analyticsScope": "landing_page" if (analytics or {}).get("connected") else "missing",
        "schemaNote": package.get("_schemaNote") if isinstance(package, dict) else "",
    }
    if isinstance(package, dict):
        package.pop("_schemaNote", None)
    return {
        "pageType": kind,
        "validation": validation,
        "openIssues": open_issues,
        "beatsLive": beats_live,
        "recommendKeep": recommend_keep,
        "meetCompetitor": {k: meet.get(k) for k in ("title", "url", "description", "source")} if meet else {},
        "jev": jev,
        "analysis": analysis,
        "title": title,
        "metaDescription": desc,
        "openGraphTitle": og_title,
        "openGraphDescription": og_desc,
        "shareImage": share,
        "images": images,
        "canonical": scraped.get("canonical") or "",
        "canonicalChanged": False,
        "nav": scraped.get("nav") or [],
        "faqs": scraped.get("faqs") or [],
        "videos": scraped.get("videos") or [],
        "reviewRating": scraped.get("reviewRating") or "",
        "reviewCount": scraped.get("reviewCount") or "",
        "jsonLd": package,
        "schemaTypesPreserved": list(scraped.get("schemaTypes") or []),
        "reason": reason,
        "target": target_kw,
        "model": model,
        "role": role,
        "draftBody": text,
        "scraped": {**scraped, "headings": headings or ""},
        "competitors": rivals,
        "queries": queries,
        "analytics": analytics or {},
        "specificity": (specificity or "balanced").strip().lower() or "balanced",
        "objective": objective_key,
    }


