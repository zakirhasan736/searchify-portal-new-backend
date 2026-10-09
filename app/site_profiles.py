"""Onboarding answers stored on the server, one profile per website.

The browser keeps a copy for speed, but this is the source of truth: the scan, the title writer,
and research read the saved answers for the site's host. Changing an answer that affects writing
marks that site's scan stale so the next drafts use the new context.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app import locations
from app.models import FeatureRecord

ANSWER_KEYS = (
    "site", "platform", "businessType", "reach", "market", "shortGoal", "industry", "avoid",
    "competitors", "googleEmail", "wordpress", "gsc",
    "targetCountry", "marketSource",
)
WRITING_KEYS = ("site", "businessType", "reach", "market", "shortGoal", "industry", "avoid", "competitors")
MAX_SITES = 25


class ProfileError(ValueError):
    pass


def host_of(url: str) -> str:
    raw = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/")[0].split("?")[0]
    return raw.removeprefix("www.")


def clean_answers(raw: dict | None) -> dict:
    out = {}
    for key in ANSWER_KEYS:
        value = (raw or {}).get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = value
        else:
            out[key] = " ".join(str(value).split())[:600]
    return out


def answers_hash(answers: dict) -> str:
    data = {k: (answers.get(k) or "") for k in WRITING_KEYS}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


def brief_from_answers(answers: dict) -> dict:
    """Same shape as the frontend's briefFromAnswers, built on the server."""
    industry = answers.get("industry") or ""
    return {
        "siteUrl": answers.get("site") or "",
        "hostname": host_of(answers.get("site") or ""),
        "businessType": answers.get("businessType") or "",
        "market": answers.get("market") or "",
        "reach": answers.get("reach") or "",
        "goal": answers.get("shortGoal") or "",
        "avoid": answers.get("avoid") or "",
        "sensitive": "" if industry in {"", "No special category"} else industry,
        "platform": answers.get("platform") or "",
        "competitors": answers.get("competitors") or "",
    }


def location_status(answers: dict) -> dict:
    try:
        place = locations.resolve(answers.get("market") or "", reach=answers.get("reach") or "", allow_worldwide=True)
    except locations.LocationError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, **place.to_dict(), "label": place.label}


def _record(db: Session, user_id: int, kind: str, title: str) -> FeatureRecord | None:
    return (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind, FeatureRecord.title == title)
        .order_by(FeatureRecord.id.desc())
        .first()
    )


def _clean_state(state: dict) -> dict:
    sites = []
    for site in (state or {}).get("sites") or []:
        if not isinstance(site, dict) or site.get("id") in (None, ""):
            continue
        status = site.get("status") if site.get("status") in {"draft", "ready"} else "draft"
        sites.append({
            "id": site["id"],
            "status": status,
            "answers": clean_answers(site.get("answers")),
            "createdAt": site.get("createdAt") or site["id"],
            **({"step": int(site["step"])} if isinstance(site.get("step"), int) else {}),
        })
    if len(sites) > MAX_SITES:
        raise ProfileError(f"A workspace can hold up to {MAX_SITES} websites.")
    plan = (state or {}).get("planId")
    return {
        "planId": plan if plan in {"starter", "growth", "agency", None} else None,
        "billing": "annual" if (state or {}).get("billing") == "annual" else "monthly",
        "sites": sites,
        "activeId": (state or {}).get("activeId"),
    }


def get_journey(db: Session, user_id: int) -> dict | None:
    row = _record(db, user_id, "journey", "Onboarding journey")
    return dict(row.payload) if row and row.payload else None


def save_journey(db: Session, user_id: int, state: dict) -> dict:
    clean = _clean_state(state)
    now = datetime.utcnow().isoformat(timespec="seconds")
    row = _record(db, user_id, "journey", "Onboarding journey")
    payload = {"state": clean, "updatedAt": now}
    if row is None:
        db.add(FeatureRecord(customer_id=user_id, kind="journey", title="Onboarding journey", payload=payload, status="stored"))
    else:
        row.payload = payload
        row.status = "stored"

    profiles = []
    for site in clean["sites"]:
        answers = site["answers"]
        host = host_of(answers.get("site") or "")
        if site["status"] != "ready" or not host:
            continue
        digest = answers_hash(answers)
        title = f"Site profile {host}"
        existing = _record(db, user_id, "site-profile", title)
        before = dict((existing.payload if existing else {}) or {})
        changed = before.get("answersHash") != digest
        profile = {
            "host": host,
            "siteId": site["id"],
            "answers": answers,
            "brief": brief_from_answers(answers),
            "location": location_status(answers),
            "answersHash": digest,
            "updatedAt": now if changed else before.get("updatedAt") or now,
        }
        if existing is None:
            db.add(FeatureRecord(customer_id=user_id, kind="site-profile", title=title, payload=profile, status="stored"))
        else:
            existing.payload = profile
        if changed and before:
            scan = _record(db, user_id, "site-scan", f"Site scan {host}")
            if scan is not None and scan.payload:
                scan.payload = {**scan.payload, "stale": True, "staleReason": "Setup answers changed"}
        profiles.append({"host": host, "changed": changed, "location": profile["location"]})
    db.commit()
    return {"journey": payload, "profiles": profiles}


def profile_for_host(db: Session, user_id: int, host: str) -> dict | None:
    if not host:
        return None
    row = _record(db, user_id, "site-profile", f"Site profile {host_of(host)}")
    return dict(row.payload) if row and row.payload else None


def conflicts(brief: dict, scan: dict) -> list[dict]:
    """Where the setup answers and the website disagree. Deterministic, from the crawl only."""
    out: list[dict] = []
    if not brief or not scan or not scan.get("pages"):
        return out
    pages = scan.get("pages") or []
    corpus = " ".join(
        " ".join(str(p.get(k) or "") for k in ("title", "h1", "description", "text")) for p in pages
    ).lower()
    corpus += " " + " ".join(map(str, (scan.get("business") or {}).get("places") or [])).lower()
    market = brief.get("market") or ""
    if market:
        try:
            place = locations.resolve(market, reach=brief.get("reach") or "", allow_worldwide=True)
        except locations.LocationError as exc:
            out.append({"field": "market", "setup": market, "site": "", "message": str(exc)})
        else:
            names = [n for n in (place.city, place.region) if n]
            if names and not any(n.lower() in corpus for n in names):
                out.append({
                    "field": "market", "setup": market, "site": "",
                    "message": f"Setup says you serve {place.label}, but none of the {len(pages)} pages read mention {names[0]}.",
                })
    roles = [((scan.get("pageMap") or {}).get(p["url"]) or {}).get("role") for p in pages]
    services = sum(1 for r in roles if r == "service")
    products = sum(1 for r in roles if r in {"product", "category"})
    model = (brief.get("businessType") or "").lower()
    if model == "products" and services >= 3 and products == 0:
        out.append({"field": "businessType", "setup": "Products", "site": f"{services} service pages",
                    "message": "Setup says you sell products, but the pages read describe services."})
    if model == "services" and products >= 3 and services == 0:
        out.append({"field": "businessType", "setup": "Services", "site": f"{products} product pages",
                    "message": "Setup says you sell services, but the pages read are product or category pages."})
    own = scan.get("host") or ""
    for rival in re.split(r"[\s,]+", brief.get("competitors") or ""):
        if rival and own and host_of(rival) == own:
            out.append({"field": "competitors", "setup": rival, "site": own, "message": "One of the competitors you named is your own website."})
    return out
