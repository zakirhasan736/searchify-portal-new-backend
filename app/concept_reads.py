"""Turn stored Searchify SEO rows into suggestions that stay on the business offer.

Reads data that is already paid for. Does not call the provider.
"""

from __future__ import annotations

from app.meta_writer import _content_tokens


def _stem(left: str, right: str) -> bool:
    if left == right:
        return True
    return len(left) >= 5 and len(right) >= 5 and left[:5] == right[:5]


def offer_fit(term: str, offers: list[str]) -> tuple[float, str]:
    """How much of the phrase belongs to one offer. 1.0 means every word matches."""
    words = _content_tokens(term)
    if not words or not offers:
        return 0.0, ""
    best_score, best_offer = 0.0, ""
    for offer in offers:
        topic = _content_tokens(offer)
        if not topic:
            continue
        shared = sum(1 for word in words if any(_stem(word, other) for other in topic))
        score = shared / len(words)
        if score > best_score:
            best_score, best_offer = score, offer
    return best_score, best_offer


def _site_business(db, user_id: int, host: str) -> dict:
    if db is None or not user_id or not host:
        return {}
    from app.models import FeatureRecord

    row = (
        db.query(FeatureRecord)
        .filter(
            FeatureRecord.customer_id == user_id,
            FeatureRecord.kind == "site-scan",
            FeatureRecord.title == f"Site scan {host}",
        )
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict((getattr(row, "payload", None) or {}).get("business") or {}) if row else {}


def business_offers(db, user_id: int, host: str) -> list[str]:
    """Services/products named when the site was read. Empty when the site has not been read.

    The one-sentence business summary is NOT an offer: used as a keyword seed it became
    “Acme Plumbing is a family owned” and returned no useful searches. It is used only when the
    site read found no offers at all.
    """
    business = _site_business(db, user_id, host)
    offers: list[str] = []
    for value in business.get("offers") or []:
        text = " ".join(str(value or "").split())
        if text and text.lower() not in {item.lower() for item in offers}:
            offers.append(text[:80])
    if not offers and business.get("business"):
        offers.append(" ".join(str(business["business"]).split())[:80])
    return offers[:15]


def business_places(db, user_id: int, host: str) -> list[str]:
    """Cities/regions the site says it serves (from the site read)."""
    places = []
    for value in _site_business(db, user_id, host).get("places") or []:
        text = " ".join(str(value or "").split())
        if text and text.lower() not in {item.lower() for item in places}:
            places.append(text[:60])
    return places[:6]


def seed_phrase(offer: str) -> str:
    return " ".join(str(offer or "").split()[:6])[:80]


def _volume_clause(volume) -> str:
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or volume <= 0:
        return ""
    return f" About {int(volume):,} searches a month in the target country."


def keyword_concepts(payload: dict | None, offers: list[str] | None) -> dict:
    data = payload or {}
    offers = [offer for offer in (offers or []) if str(offer).strip()]
    rows: list[dict] = []
    seen: set[str] = set()

    def add(row: dict) -> None:
        keyword = " ".join(str(row.get("keyword") or "").split())
        key = keyword.lower()
        if not key or key in seen:
            return
        score, offer = offer_fit(keyword, offers) if offers else (0.0, "")
        if offers and score < 0.34:
            return
        seen.add(key)
        position = row.get("position")
        volume = row.get("volume")
        seed = " ".join(str(row.get("seed") or "").split())
        if offer:
            concept = offer
            if isinstance(position, int):
                why = f"This search matches {offer}. The site already appears around position {position}."
            else:
                why = f"This search matches {offer}, and the site is not in the top 100 for it yet."
                if seed and seed.lower() != keyword.lower():
                    why += f" It came from the offer “{seed}”."
            why += _volume_clause(volume)
        else:
            concept = seed or "Tracked term"
            why = f"Suggested from “{concept}”." + _volume_clause(volume)
            why += " Read the site so the next refresh keeps only searches that match what the business sells."
        rows.append({
            "keyword": keyword,
            "title": keyword,
            "concept": concept,
            "why": why,
            "volume": volume if isinstance(volume, (int, float)) and not isinstance(volume, bool) else None,
            "difficulty": row.get("difficulty"),
            "intent": row.get("intent") or "",
            "position": position if isinstance(position, int) else None,
            "seed": seed,
        })

    for idea in data.get("ideas") or []:
        if isinstance(idea, dict):
            add(idea)
        if len(rows) >= 8:
            break
    if offers and len(rows) < 8:
        for ranked in data.get("ranked") or []:
            if isinstance(ranked, dict):
                add(ranked)
            if len(rows) >= 8:
                break

    if not (data.get("ideas") or data.get("ranked")):
        note = "No keyword suggestions are stored yet. Refresh live keyword data."
    elif offers and not rows:
        named = ", ".join(offers[:3])
        note = (
            f"The stored searches do not match {named}. "
            "Refresh live keyword data. The next suggestions start from those offers."
        )
    elif not offers:
        note = "These follow the terms already tracked. Read the site on Titles so the next refresh stays on the offer."
    else:
        note = "Searches that match the offer, from the Searchify SEO data already stored for this website."
    return {"rows": rows, "note": note}


def visibility_reading(row: dict | None, offers: list[str] | None) -> dict:
    data = row or {}
    question = " ".join(str(data.get("text") or "").split())
    _score, offer = offer_fit(question, offers or [])
    concept = offer or "this question"
    if data.get("status") not in (None, "ok", "empty"):
        return {"concept": concept, "reading": "", "next": ""}
    others = []
    for source in data.get("sources") or []:
        if not isinstance(source, dict):
            continue
        title = " ".join(str(source.get("title") or source.get("url") or "").split())
        if title:
            others.append(title[:120])
    mentioned = data.get("mention") is True
    cited = bool(data.get("citationUrl") or data.get("citation"))
    if data.get("status") == "empty" or (data.get("mention") is None and not data.get("snippet")):
        return {
            "concept": concept,
            "reading": "This question has no answer stored yet.",
            "next": "Run the live check. Searchify SEO asks the engine and stores the answer.",
        }
    if mentioned and cited:
        reading = "The answer names the business and links to the site."
        nxt = "Keep the page that was cited. A later check shows whether that mention stays."
    elif mentioned:
        reading = "The answer names the business, without a link to the site."
        nxt = f"The page about {concept} needs a title and description that match this question, so the answer has a page to cite."
    else:
        reading = "The answer does not name the business."
        if others:
            reading += f" It points at {others[0]}."
        nxt = f"This question is about {concept}. The matching page should say that in the title and the opening paragraph."
    return {"concept": concept, "reading": reading, "next": nxt}


def backlink_concepts(payload: dict | None, offers: list[str] | None) -> dict:
    data = payload or {}
    offers = [offer for offer in (offers or []) if str(offer).strip()]
    links = [link for link in (data.get("links") or []) if isinstance(link, dict)]
    rows: list[dict] = []
    on_topic = 0
    for link in links:
        anchor = " ".join(str(link.get("anchor") or "").split())
        score, offer = offer_fit(anchor, offers) if offers and anchor else (0.0, "")
        domain = link.get("domain") or "A referring domain"
        target = link.get("target") or "the site"
        state = link.get("state") or "Active"
        if offers and score >= 0.34 and state == "Lost":
            rows.append({
                "title": domain,
                "concept": offer,
                "why": (
                    f"The anchor “{anchor}” matches {offer}, and the link is marked lost. "
                    "Open the source page before deciding anything."
                ),
            })
        elif offers and score >= 0.34:
            on_topic += 1
            rows.append({
                "title": domain,
                "concept": offer,
                "why": f"This {state.lower()} link points at {target} with the words “{anchor}”. Those words match {offer}.",
            })
        elif not offers and anchor and state == "Lost":
            rows.append({
                "title": domain,
                "concept": "Lost link",
                "why": (
                    f"The anchor was “{anchor}”. It is marked lost. Open the source page before outreach. "
                    "Searchify does not remove the link."
                ),
            })
        if len(rows) >= 8:
            break
    if offers and on_topic == 0 and links:
        rows.insert(0, {
            "title": offers[0],
            "concept": offers[0],
            "why": (
                f"None of the listed anchors match {offers[0]}. The link list is real, and it does not yet describe that offer. "
                "A useful link would use those words and point at the page that sells it."
            ),
        })
    if not links:
        note = "No referring domains are stored yet. Refresh live backlink data."
        rows = []
    elif not offers:
        note = "These are the stored links. Read the site so this view can mark which anchors match the offer."
    else:
        note = "Links whose words match the offer. Searchify does not build or remove links."
    return {"rows": rows[:8], "note": note}
