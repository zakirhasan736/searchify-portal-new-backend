"""Page records for title/description drafts: page type, content status, claim checks, confidence.

Validation (are the facts in the draft supported by the page?) is kept separate from confidence
(how much evidence the draft was written from). A draft can be high-confidence and still contain
an unverified claim, and that must be visible to the reviewer.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

THIN_WORDS = 120
CASE_STUDY = re.compile(r"(case[-_ ]?stud|portfolio|our[-_ ]work|projects?/|success[-_ ]stor|client[-_ ]stor|testimonial)", re.I)
BLOG = re.compile(r"(/blog/|/news/|/articles?/|/posts?/|/\d{4}/\d{2}/)", re.I)
LEGAL = re.compile(r"(privacy|cookie|terms|disclaimer|refund|legal|gdpr|accessibility)", re.I)
PRODUCT = re.compile(r"(/products?/|/shop/|/store/|/item/)", re.I)
SERVICE = re.compile(r"(/services?/|/solutions?/|/offerings?/)", re.I)
LOCATION = re.compile(r"(/locations?/|/areas?-we-serve|/near-me|/in-[a-z-]+)", re.I)
CATEGORY = re.compile(r"(/categor(?:y|ies)/|/collections?/|/topics?/)", re.I)

NUMBER_CLAIM = re.compile(
    r"(?<![\w.])(\$?\d[\d,]*(?:\.\d+)?\+?%?)(?:\s*(years?|yrs?|clients?|customers?|projects?|reviews?|stars?|locations?|cities|hours?|days?|minutes?|mins?))?",
    re.I,
)
SPECIAL = re.compile(r"\b(24/7|same[- ]day|next[- ]day|free (?:quote|estimate|consultation|shipping|delivery)|money[- ]back|guarantee[d]?|award[- ]winning|certified|licensed|insured|accredited|#1|number one|best[- ]rated|top[- ]rated)\b", re.I)


def page_type(url: str, role: str = "", page: dict | None = None) -> str:
    path = urlparse(url or "").path.strip("/").lower()
    blob = " ".join([path, (page or {}).get("h1") or "", (page or {}).get("title") or ""])
    schema = " ".join(str(t) for t in ((page or {}).get("schemaTypes") or [])).lower()
    role = (role or "").lower()
    if role in {"home", "service", "product", "category", "location", "about", "contact", "blog", "legal", "case_study"}:
        return "home" if role == "home" else role
    if not path:
        return "home"
    if CASE_STUDY.search(blob):
        return "case_study"
    if LEGAL.search(path):
        return "legal"
    if BLOG.search(f"/{path}/"):
        return "blog"
    if PRODUCT.search(f"/{path}/") or "product" in schema:
        return "product"
    if CATEGORY.search(f"/{path}/"):
        return "category"
    if SERVICE.search(f"/{path}/") or "service" in schema:
        return "service"
    if LOCATION.search(f"/{path}/"):
        return "location"
    if role in {"service", "product", "category", "location", "about", "contact", "blog", "legal"}:
        return role
    return "other"


def content_status(page: dict | None) -> tuple[str, str]:
    """(status, plain reason). status: ok | thin | failed | noindex | non_canonical."""
    if not page:
        return "failed", "The page was not read."
    if page.get("error"):
        return "failed", f"The page could not be read ({page['error']})."
    if page.get("noindex"):
        return "noindex", "The page tells Google not to index it, so a new listing would not show."
    if page.get("canonicalElsewhere"):
        return "non_canonical", f"The page's canonical points to {page.get('canonical')}, so Google lists that URL instead."
    words = int(page.get("words") or 0)
    has_listing = bool(
        str(page.get("title") or "").strip()
        or str(page.get("h1") or "").strip()
        or str(page.get("schemaName") or "").strip()
        or str(page.get("ogTitle") or "").strip()
    )
    if words < THIN_WORDS:
        if has_listing and words >= 8:
            return "ok", ""
        return "thin", f"The page has {words} words of readable copy. A listing written from that would be a guess."
    return "ok", ""


def _number_claims(text: str) -> list[tuple[str, str]]:
    out = []
    for match in NUMBER_CLAIM.finditer(text or ""):
        value, unit = match.group(1), (match.group(2) or "").lower()
        if re.fullmatch(r"\d{4}", value) and not unit:
            continue
        out.append((value, unit))
    return out


def _digits(value: str) -> str:
    return re.sub(r"[^\d.]", "", value)


def _unit_root(unit: str) -> str:
    return re.sub(r"(s|es)$", "", unit or "")[:4]


def validate_claims(title: str, description: str, *, evidence: str, places: list[str] | None = None) -> dict:
    """Check numbers, guarantees and place names in the draft against the page/site text."""
    draft = f"{title or ''} {description or ''}"
    source = " ".join((evidence or "").split()).lower()
    claims: list[dict] = []

    page_numbers = _number_claims(source)
    for value, unit in _number_claims(draft):
        digits = _digits(value)
        same_unit = [v for v, u in page_numbers if unit and _unit_root(u) == _unit_root(unit)]
        if any(_digits(v) == digits for v, _ in page_numbers):
            status = "verified"
        elif same_unit:
            status = "contradicted"
        else:
            status = "unverified"
        claims.append({"claim": f"{value} {unit}".strip(), "kind": "number", "status": status,
                       **({"page": f"{same_unit[0]} {unit}"} if status == "contradicted" else {})})

    for match in SPECIAL.finditer(draft):
        phrase = match.group(0).lower()
        root = re.sub(r"[- ]", "", phrase)
        status = "verified" if root in re.sub(r"[- ]", "", source) else "unverified"
        claims.append({"claim": match.group(0), "kind": "promise", "status": status})

    for place in places or []:
        name = str(place or "").strip()
        if len(name) < 3:
            continue
        if re.search(rf"(?<![a-z]){re.escape(name.lower())}(?![a-z])", draft.lower()):
            status = "verified" if name.lower() in source else "unverified"
            claims.append({"claim": name, "kind": "place", "status": status})

    statuses = {c["status"] for c in claims}
    if "contradicted" in statuses:
        overall = "contradicted"
    elif not claims or statuses == {"verified"}:
        overall = "verified"
    elif "verified" in statuses:
        overall = "partially_verified"
    else:
        overall = "unverified"
    return {"status": overall, "claims": claims}


def case_study_problems(title: str, description: str, page: dict | None, brand: str = "") -> list[str]:
    """A case study is about work for a client. The listing must not present the client as the owner."""
    text = f"{title} {description}".lower()
    issues = []
    if not re.search(r"\b(case study|project|how we|for|helped|results?|work)\b", text):
        issues.append("This is a case study. Say it is work done for a client (for example “Case study:” or “How we helped …”).")
    if re.search(r"\b(we are|our (?:clinic|store|shop|company|firm|office) in)\b", text):
        issues.append("Do not describe the client's business or location as the site owner's.")
    if brand and brand.lower() not in text and len(title or "") < 45:
        issues.append(f"Make clear the work was done by {brand}.")
    return issues


_CASE_STUDY_PHRASE = re.compile(r"\b(case[- ]?stud(?:y|ies)|portfolio piece|client stor(?:y|ies)|success stor(?:y|ies))\b", re.I)
_PROPER_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_PORTFOLIO_CONTEXT = re.compile(
    r"(case[- ]?stud|portfolio|our[- ]work|selected work|client|project|testimonial|success stor)",
    re.I,
)


def _allowed_name_tokens(brand: str, places: list[str] | None, offers: list[str] | None) -> set[str]:
    blob = " ".join(
        str(part)
        for part in (
            brand,
            *(places or []),
            *(offers or []),
            "Google Ads",
            "Search Console",
            "Digital Marketing",
            "Web Design",
            "Web Development",
        )
        if part
    )
    return {part for part in re.findall(r"[a-z0-9]+", blob.lower()) if len(part) > 2}


# Title-cased headings and CTAs ("Free Consultation", "Small Business Owners", "Our Process") sit next to
# words like "work" or "client" on almost every page. They are not client names, and treating them as
# such rejected good, on-topic suggestions.
_GENERIC_NAME_WORDS = {
    "our", "your", "the", "and", "for", "with", "get", "free", "quote", "call", "contact", "book", "now", "today",
    "learn", "more", "read", "view", "see", "all", "new", "about", "home", "services", "service", "solutions",
    "solution", "business", "businesses", "small", "local", "owners", "owner", "team", "process", "approach",
    "consultation", "consult", "started", "start", "plan", "plans", "pricing", "price", "support", "care", "repair",
    "installation", "maintenance", "custom", "professional", "quality", "results", "work", "works", "case",
    "studies", "study", "project", "projects", "client", "clients", "portfolio", "selected", "featured", "recent",
    "why", "choose", "how", "what", "who", "faq", "faqs", "questions", "reviews", "review", "testimonials",
    "marketing", "digital", "website", "websites", "design", "development", "seo", "social", "media", "ads",
    "brand", "branding", "strategy", "content", "online", "search", "engine", "optimization", "management",
    "residential", "commercial", "industrial", "emergency", "company", "agency", "group", "experts", "expert",
    "years", "experience", "trusted", "best", "top", "product", "products", "shop", "store", "collection",
}


def portfolio_client_names(page_text: str, headings: str = "", *, brand: str = "", places: list[str] | None = None, offers: list[str] | None = None) -> list[str]:
    """Proper names that appear in portfolio/case-study context on the page — not the site owner."""
    allowed = _allowed_name_tokens(brand, places, offers) | _GENERIC_NAME_WORDS
    blob = f"{page_text or ''} {headings or ''}"
    found: list[str] = []
    for match in _PROPER_NAME.finditer(blob):
        name = match.group(1)
        words = {part for part in re.findall(r"[a-z0-9]+", name.lower()) if len(part) > 2}
        if not words or words <= allowed:
            continue
        # Require local portfolio/case-study cues so ordinary Proper nouns on a
        # service page are not treated as clients.
        start = max(0, match.start() - 160)
        end = min(len(blob), match.end() + 160)
        if not _PORTFOLIO_CONTEXT.search(blob[start:end]):
            continue
        if name.lower() not in {item.lower() for item in found}:
            found.append(name)
        if len(found) >= 12:
            break
    return found


def homepage_problems(
    title: str,
    description: str,
    *,
    brand: str = "",
    places: list[str] | None = None,
    offers: list[str] | None = None,
    page_text: str = "",
    headings: str = "",
) -> list[str]:
    """Homepage listings describe the business. They must not become a case-study card."""
    draft = f"{title or ''} {description or ''}"
    issues: list[str] = []
    if _CASE_STUDY_PHRASE.search(draft):
        issues.append("This is the homepage. Do not write a case-study title or description.")
    clients = portfolio_client_names(page_text, headings, brand=brand, places=places, offers=offers)
    draft_lower = draft.lower()
    for name in clients:
        if name.lower() in draft_lower:
            issues.append(
                f"Do not name the portfolio client “{name}” on the homepage listing. "
                "Keep the listing about the business offer and place."
            )
            break
    # Catch case-study shaped titles even when the client name was not scraped as such.
    if re.search(r"\|\s*.+\s+case\s*stud", draft, re.I) or re.search(r"\bcase\s*stud(?:y|ies)\s*[:|]?", draft, re.I):
        if "This is the homepage" not in " ".join(issues):
            issues.append("This is the homepage. Do not write a case-study title or description.")
    return issues


def service_page_problems(
    title: str,
    description: str,
    *,
    topic: str,
    page_text: str = "",
    headings: str = "",
    brand: str = "",
    places: list[str] | None = None,
    offers: list[str] | None = None,
) -> list[str]:
    """Service/product pages must stay on their own offer — not a sibling case study."""
    draft = f"{title or ''} {description or ''}"
    issues: list[str] = []
    if _CASE_STUDY_PHRASE.search(draft) and not _CASE_STUDY_PHRASE.search(f"{topic} {page_text}"):
        issues.append("This page is not a case study. Keep the listing on this page's service or product.")
    clients = portfolio_client_names(page_text, headings, brand=brand, places=places, offers=offers)
    draft_lower = draft.lower()
    topic_lower = (topic or "").lower()
    for name in clients:
        if name.lower() in draft_lower and name.lower() not in topic_lower:
            issues.append(f"Do not bring in the unrelated client “{name}”. Stay on this page's subject.")
            break
    return issues


def confidence(*, content: str, gsc: dict | None, rivals: list | None, validation: str, analytics: bool = False) -> dict:
    """How much evidence the draft rests on.

    Coverage (this score) is separate from claim validity. A draft can be high-coverage
    and still contain an unverified claim — callers must surface ``validation`` separately.
    Missing GSC/Analytics never counts as zero performance; it simply adds no evidence.
    """
    score, basis, missing = 0, [], []
    if content == "ok":
        score += 2
        basis.append("page copy")
    else:
        missing.append("readable page copy" if content != "ok" else "")
    gsc = gsc or {}
    impressions = gsc.get("impressions")
    if impressions is not None and str(impressions).strip() not in {"", "—", "None", "null"}:
        score += 2
        basis.append("Search Console")
    else:
        missing.append("Search Console for this URL")
    if rivals:
        score += 1
        basis.append("competitor listings")
    else:
        missing.append("same-intent competitor listings")
    if analytics:
        score += 1
        basis.append("Analytics")
    else:
        missing.append("Analytics landing metrics")
    # Do not inflate confidence when claims are contradicted — coverage stays, level capped.
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    if validation == "contradicted" and level == "high":
        level = "medium"
        missing.append("claim validation passed")
    return {
        "level": level,
        "basis": basis,
        "missing": [item for item in missing if item],
        "validationStatus": validation or "",
        "note": "Confidence reflects evidence coverage, not predicted rankings or traffic.",
    }
