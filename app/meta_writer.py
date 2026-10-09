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
    "You are a senior SEO copywriter. You write the Google title and meta description for one specific page "
    "after reading the whole website, the owner's brief, and the competitor listings that rank for the same search. "
    "Work in this order before you write: (1) name the one search this page should win; "
    "(2) note what the competitor titles all say, so you can say something they do not; "
    "(3) pick the strongest proof on this page or the site (a real fact such as same-day, free quote, a named area, a product range); "
    "(4) write a listing a searcher would click over the competitors. "
    "A strong listing puts the target search near the front, names the real offer and the place or audience, "
    "and gives one concrete reason to click. "
    "Every page on the site needs a different title: never reuse the angle of another page on the same site. "
    "It does not sound like an agency slogan. "
    "Do not invent reviews, years in business, prices, awards, or certifications. "
    "Do not copy a competitor title. "
    "Do not use filler such as welcome, trusted, discover, unlock, elevate, seamless, leading, or one-stop. "
    "Title: 50–60 characters, the offer first, the brand only at the end if it still fits. "
    "Description: 140–155 characters. It must not repeat the title. Say who it is for, one fact from the page, and the next step in plain language. "
    "If the page is not about the first Search Console query, use the query this page can honestly match. "
    "Search Console and Analytics are evidence for why the listing should change. "
    "Do not put click counts, session counts, or bounce rates inside the title or description. "
    "Return exactly four lines:\n"
    "TITLE: ...\n"
    "DESCRIPTION: ...\n"
    "TARGET: the search this listing is written to win\n"
    "REASON: one sentence: the page detail you used and how it beats the competitor listings."
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
    if current_title and _overlap(title, current_title) >= 0.72:
        issues.append("The title is too close to the current title. Change the angle, not just a word.")
    if title and desc and _overlap(title, desc) >= 0.7:
        issues.append("The description repeats the title. Add a fact and a next step instead.")
    page_words = _tokens(page_bits)
    title_words = _tokens(title)
    if page_words and title_words and len(title_words & page_words) < 2:
        issues.append("The title does not use the offer or place that is actually on this page.")
    if title.lower().startswith(("home", "welcome", "untitled")):
        issues.append("Do not start the title with Home or Welcome.")
    return issues[:6]


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
    query_block = "\n".join(f"- {q}" for q in queries) or "(No Search Console queries yet.)"
    page_bits = " ".join(
        part for part in (scraped.get("h1"), headings, scraped.get("title"), scraped.get("text")) if part
    )
    if site and site.get("business"):
        business = site["business"]
        page_bits += " " + " ".join(map(str, [*(business.get("offers") or []), *(business.get("places") or [])]))

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
        f"Current title: {scraped.get('title') or '(none)'}\n"
        f"Current description: {scraped.get('description') or '(none)'}\n"
        f"H1: {scraped.get('h1') or '(none)'}\n"
        f"Section headings: {headings or '(none)'}\n"
        f"Page copy:\n{(scraped.get('text') or scraped.get('error') or '(could not read the page)')[:3200]}\n\n"
        f"Search Console queries for the site. Pick the one this page can honestly match:\n{query_block}\n"
        f"This URL in Search Console: clicks={gsc.get('clicks')} impressions={gsc.get('impressions')} position={gsc.get('position')}\n\n"
        f"Analytics for the property, last stored window. Use a figure only if it is written here:\n{_analytics_block(analytics)}\n\n"
        f"{specificity_instruction(specificity)}\n\n"
        f"Competitor pages for this page's search. Beat them: be more specific, give a clearer reason to click. "
        f"Do not copy them. Ignore any with a different intent:\n{_rival_block(rivals)}\n"
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
            + "Write a new listing that fixes every check. Keep a real detail from the page copy."
        )
        retry_title, retry_desc, retry_reason = _parse_meta(retry_text)
        retry_issues = checks(retry_title, retry_desc)
        if len(retry_issues) <= len(issues) and retry_title and retry_desc:
            title, desc, reason = retry_title, retry_desc, retry_reason
            text, model, role = retry_text, retry_model, retry_role
    if not reason:
        reason = "Written from this page’s heading, the rest of the site, and the search it can win."
    validation = page_quality.validate_claims(title, desc, evidence=evidence_text, places=places)
    return {
        "pageType": kind,
        "validation": validation,
        "openIssues": checks(title, desc) if title else ["The writer returned no title."],
        "title": title,
        "metaDescription": desc,
        "reason": reason,
        "target": _parse_target(text) or ((site or {}).get("page") or {}).get("target") or "",
        "model": model,
        "role": role,
        "draftBody": text,
        "scraped": {**scraped, "headings": headings or ""},
        "competitors": rivals,
        "queries": queries,
        "analytics": analytics or {},
        "specificity": (specificity or "balanced").strip().lower() or "balanced",
    }


