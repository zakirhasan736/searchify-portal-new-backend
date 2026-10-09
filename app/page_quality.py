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

NUMBER_CLAIM = re.compile(
    r"(?<![\w.])(\$?\d[\d,]*(?:\.\d+)?\+?%?)(?:\s*(years?|yrs?|clients?|customers?|projects?|reviews?|stars?|locations?|cities|hours?|days?|minutes?|mins?))?",
    re.I,
)
SPECIAL = re.compile(r"\b(24/7|same[- ]day|next[- ]day|free (?:quote|estimate|consultation|shipping|delivery)|money[- ]back|guarantee[d]?|award[- ]winning|certified|licensed|insured|accredited|#1|number one|best[- ]rated|top[- ]rated)\b", re.I)


def page_type(url: str, role: str = "", page: dict | None = None) -> str:
    path = urlparse(url or "").path.strip("/").lower()
    blob = " ".join([path, (page or {}).get("h1") or "", (page or {}).get("title") or ""])
    if not path:
        return "home"
    if CASE_STUDY.search(blob):
        return "case_study"
    if LEGAL.search(path):
        return "legal"
    if BLOG.search(f"/{path}/"):
        return "blog"
    role = (role or "").lower()
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
    if words < THIN_WORDS:
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


def confidence(*, content: str, gsc: dict | None, rivals: list | None, validation: str, analytics: bool = False) -> dict:
    """How much evidence the draft rests on. Independent of whether the claims check out."""
    score, basis = 0, []
    if content == "ok":
        score += 2
        basis.append("page copy")
    gsc = gsc or {}
    if str(gsc.get("impressions") or "").strip() not in {"", "—", "None"}:
        score += 2
        basis.append("Search Console")
    if rivals:
        score += 1
        basis.append("competitor listings")
    if analytics:
        score += 1
        basis.append("Analytics")
    level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    return {"level": level, "basis": basis}
