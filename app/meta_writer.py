"""Page-specific Google title and description. One rewrite if the first draft is generic."""

from __future__ import annotations

import re

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
    "You are a senior SEO copywriter. You write the Google title and meta description for one specific page. "
    "The page topic is the concept. The best keyword is the search this page can honestly win. "
    "Work in this order: (1) read the page topic in the brief and stay on that concept — do not switch to another service from the business brief or from a popular site-wide query; "
    "(2) put the best keyword, in those words, near the front of the title; "
    "(3) add one concrete fact from this page (a place, an offer, a proof) so the listing is specific; "
    "(4) look at competitor titles only to avoid copying them. Do not change the topic just to sound different. "
    "A strong listing is conceptual and keyword-clear: a searcher can tell what the page is about in the first words. "
    "Every page on the site needs its own topic: never reuse another page's title. "
    "It does not sound like an agency slogan. "
    "Do not invent reviews, years in business, prices, awards, or certifications. "
    "Do not copy a competitor title. "
    "Do not use filler such as welcome, trusted, discover, unlock, elevate, seamless, leading, or one-stop. "
    "Title: 50–60 characters. Keyword and topic first. Brand only at the end if it still fits. "
    "Description: 140–155 characters. It must not repeat the title word for word. "
    "Say who the page is for, the same topic in a full sentence, one fact from the page, and a plain next step. "
    "The same title and description are the Open Graph title, the Open Graph description, and the WebPage schema name and description. "
    "Read the current title, meta description, Open Graph, and JSON-LD. Stay on that page's concept. "
    "A one-word H1 such as Home is not the topic — use the schema name, the Open Graph title, or the real heading. "
    "Search Console, Analytics, and the stored Searchify SEO keywords, competitors, AI visibility, and backlinks explain which search this page can win. "
    "Do not invent search volume, positions, or mentions that are not in the brief. "
    "Do not put click counts, session counts, or bounce rates inside the title or description. "
    "Return exactly four lines:\n"
    "TITLE: ...\n"
    "DESCRIPTION: ...\n"
    "TARGET: the best keyword this listing is written to win\n"
    "REASON: one sentence naming the page topic and the keyword you used."
)


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
        if rival.get("title") and _overlap(title, rival["title"]) >= 0.6:
            issues.append("The title is too close to a competitor title. Say what they do not say.")
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
    from app.listing_research import keyword_candidates, ranked_on_url, research_lines

    page_url = scraped.get("url") or url
    ranked_rows = ((research or {}).get("keywords") or {}).get("ranked") or []
    on_page = ranked_on_url(ranked_rows, page_url)
    extra_terms = keyword_candidates(research, page_url, topic)
    if on_page:
        keyword = str(on_page[0].get("keyword") or "").strip() or topic
        matched = matching_queries([keyword, *extra_terms, *queries], topic, page_only)
        if keyword.lower() not in {item.lower() for item in matched}:
            matched = [keyword, *matched][:5]
    else:
        matched = matching_queries([*extra_terms, *queries], topic, page_only)
        keyword = best_keyword(matched, topic)
    query_block = "\n".join(f"- {q}" for q in matched) or "(No Search Console query matches this page. Use the page topic as the keyword.)"
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
        "queries": queries,
        "analytics": analytics or {},
        "target": "",
    }
    if not openai_configured():
        return empty

    short_note = ""
    if len(str(scraped.get("text") or "").split()) < 120:
        short_note = (
            "The body copy on this page is short. Write from the whole-site understanding, this page's title, heading, Open Graph, and schema. "
            "Do not invent a service that is not in the site offers.\n"
        )
    brief = (
        f"Business: {profile.get('business') or ''}\n"
        f"Services: {profile.get('services') or ''}\n"
        f"Places: {profile.get('areas') or profile.get('locations') or ''}\n"
        f"Owner's 90-day aim: {profile.get('goal') or '(not given)'}\n"
        f"Voice: {profile.get('voice') or 'specific and concrete'}\n"
        f"Only claim what is on the page or the site. Allowed notes: {profile.get('claims') or 'visible page facts only'}\n"
        f"Do not say: {profile.get('restrictions') or profile.get('rules') or 'invented awards, prices, or results'}\n\n"
        f"What the whole site says:\n{_site_block(site)}\n\n"
        f"Page: {scraped.get('url') or url}\n"
        f"PAGE TOPIC (the concept — the title and description must be about this, not another service): {topic or '(use the page heading)'}\n"
        f"BEST KEYWORD (use these words near the front of the title): {keyword or topic or '(use the page topic)'}\n"
        f"What this page already publishes:\n{current_listing}\n"
        f"Stored Searchify SEO research for this business and this page:\n{research_lines(research, page_url, topic)}\n"
        f"H1: {scraped.get('h1') or '(none)'}\n"
        f"Section headings: {headings or '(none)'}\n"
        f"Page copy:\n{(scraped.get('text') or scraped.get('error') or '(could not read the page)')[:3200]}\n"
        f"{short_note}\n"
        f"Search Console queries that match THIS page. Do not write for a query that was left out:\n{query_block}\n"
        f"This URL in Search Console: clicks={gsc.get('clicks')} impressions={gsc.get('impressions')} position={gsc.get('position')}\n\n"
        f"Analytics for the property, last stored window. Use a figure only if it is written here:\n{_analytics_block(analytics)}\n\n"
        f"{specificity_instruction(specificity)}\n\n"
        f"Competitor pages. Do not copy their titles, and do not change this page's topic to sound different from them:\n{_rival_block(rivals)}\n"
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
    evidence_text = " ".join(
        str(part) for part in (scraped.get("title"), scraped.get("description"), scraped.get("h1"), headings, scraped.get("text")) if part
    )
    places = list(((site or {}).get("business") or {}).get("places") or [])

    def checks(t: str, d: str) -> list[str]:
        found = _issues(t, d, current, page_bits, taken, rivals)
        found.extend(topic_issues(t, d, topic, keyword))
        claims = page_quality.validate_claims(t, d, evidence=evidence_text, places=places)
        for claim in claims["claims"]:
            if claim["status"] == "contradicted":
                found.append(f"“{claim['claim']}” contradicts the page, which says {claim.get('page')}. Use the page's figure or drop it.")
            elif claim["status"] == "unverified" and claim["kind"] in {"number", "promise"}:
                found.append(f"“{claim['claim']}” is not on this page. Remove it unless the page says it.")
        if kind == "case_study":
            found.extend(page_quality.case_study_problems(t, d, scraped, brand))
        return found[:8]

    text, model, role = _ask(brief + "\nWrite the listing for this page only.")
    title, desc, reason = _parse_meta(text)
    issues = checks(title, desc)
    if issues:
        retry_text, retry_model, retry_role = _ask(
            brief
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
    if not reason:
        reason = "Written from this page’s heading, the rest of the site, and the search it can win."
    validation = page_quality.validate_claims(title, desc, evidence=evidence_text, places=places)
    from app.cms_connectors import build_webpage_json_ld
    from app.page_scrape import suggest_images

    images = suggest_images(scraped.get("images"), topic, page_url)
    share = str(scraped.get("ogImage") or "").strip() or next((row["src"] for row in images if row.get("src")), "")
    package = build_webpage_json_ld(
        meta_title=title,
        meta_description=desc,
        page_url=page_url,
        site_name=brand,
    ) if title or desc else {}
    if share and package:
        package["image"] = share
    return {
        "pageType": kind,
        "validation": validation,
        "openIssues": checks(title, desc) if title else ["The writer returned no title."],
        "title": title,
        "metaDescription": desc,
        "openGraphTitle": title,
        "openGraphDescription": desc,
        "shareImage": share,
        "images": images,
        "canonical": scraped.get("canonical") or "",
        "nav": scraped.get("nav") or [],
        "faqs": scraped.get("faqs") or [],
        "videos": scraped.get("videos") or [],
        "reviewRating": scraped.get("reviewRating") or "",
        "reviewCount": scraped.get("reviewCount") or "",
        "jsonLd": package,
        "reason": reason,
        "target": _parse_target(text) or keyword or ((site or {}).get("page") or {}).get("target") or "",
        "model": model,
        "role": role,
        "draftBody": text,
        "scraped": {**scraped, "headings": headings or ""},
        "competitors": rivals,
        "queries": queries,
        "analytics": analytics or {},
        "specificity": (specificity or "balanced").strip().lower() or "balanced",
    }


