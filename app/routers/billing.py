"""Public pricing checkout + enterprise quote inbox (no Stripe yet — plan is recorded on the account)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CmsConnection, FeatureRecord, User
from app.security import get_current_user, get_optional_user

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

# Public catalog mirrors V8 Pricing.dc.html; canonical ids match journey / quotas.
CATALOG = [
    {
        "id": "solo",
        "canonical": "starter",
        "name": "Solo",
        "tag": "ONE BUSINESS",
        "sites": "1 website",
        "siteLimit": 1,
        "mo": 29,
        "features": [
            "Search Console + WordPress",
            "Colour-coded task board",
            "One move at a time, with results",
            "Change log, one-click undo, unlimited CSV export",
            "1 branded PDF report per month",
        ],
    },
    {
        "id": "pro",
        "canonical": "growth",
        "name": "Pro",
        "tag": "GROWING TEAMS",
        "sites": "Up to 5 websites",
        "siteLimit": 5,
        "mo": 79,
        "features": [
            "Everything in Solo",
            "Calls and leads from Analytics",
            "5 branded reports per site, per month",
            "1 scheduled client send + 3 client logins per site",
            "AI answer tracking and autopilot within limits",
        ],
    },
    {
        "id": "premium",
        "canonical": "agency",
        "name": "Premium",
        "tag": "BEST FOR AGENCIES",
        "sites": "Up to 25 websites",
        "siteLimit": 25,
        "mo": 399,
        "features": [
            "Everything in Pro",
            "Unlimited white-label reports, no Searchify name",
            "Unlimited scheduled sends and client logins",
            "Social channels via Postiz",
            "All-clients view and team seats",
        ],
    },
    {
        "id": "enterprise",
        "canonical": "custom",
        "name": "Enterprise",
        "tag": "LARGE PORTFOLIOS",
        "sites": "25+ websites",
        "siteLimit": 0,
        "mo": 0,
        "features": [
            "Everything in Premium",
            "Custom site and report limits",
            "Single sign-on and audit log",
            "Dedicated success manager",
            "API access and custom contract",
        ],
    },
]

BY_ID = {p["id"]: p for p in CATALOG}
BY_CANONICAL = {p["canonical"]: p for p in CATALOG if p["canonical"] != "custom"}
# Stored account plans that are not catalog ids still land on a card.
STORED_TO_CATALOG = {
    "starter": "solo",
    "solo": "solo",
    "growth": "pro",
    "pro": "pro",
    "agency": "premium",
    "premium": "premium",
    "scale": "premium",
    "custom": "enterprise",
    "enterprise": "enterprise",
}


class CheckoutBody(BaseModel):
    planId: str
    billing: str = "monthly"
    email: str = ""


class EnterpriseQuoteBody(BaseModel):
    email: str
    sites: str = Field(default="60")
    note: str = ""


def _feature(db: Session, user_id: int, kind: str) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict((row.payload if row else {}) or {})


def _save_feature(db: Session, user_id: int, kind: str, title: str, payload: dict) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if row is None:
        row = FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload)
        db.add(row)
    else:
        row.payload = payload
        row.title = title
    db.commit()
    return payload


def _admin(db: Session) -> User | None:
    return db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id).first()


@router.get("/catalog")
def catalog():
    return {"ok": True, "live": True, "plans": CATALOG, "trialDays": 14}


@router.get("/account")
def account(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stored = (user.plan or "starter").strip().lower()
    catalog_id = STORED_TO_CATALOG.get(stored, "solo")
    plan = BY_ID.get(catalog_id) or CATALOG[0]
    checkout = _feature(db, user.id, "billing-checkout")
    from app import site_profiles

    journey = site_profiles.get_journey(db, user.id) or {}
    state = journey.get("state") or {}
    ready = sum(1 for site in (state.get("sites") or []) if isinstance(site, dict) and site.get("status") == "ready")
    connected = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).count()
    billing = checkout.get("billing") or state.get("billing") or "monthly"
    if billing not in ("monthly", "annual"):
        billing = "monthly"
    return {
        "ok": True,
        "live": True,
        "planId": plan["id"],
        "canonical": stored,
        "name": plan["name"],
        "billing": billing,
        "siteLimit": int(user.site_limit or plan["siteLimit"] or 1),
        "sitesUsed": max(ready, connected),
        "email": user.email or "",
        "status": checkout.get("status") or "active",
        "trialDays": int(checkout.get("trialDays") or 0),
    }


@router.post("/checkout")
def checkout(body: CheckoutBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    raw = (body.planId or "").strip().lower()
    plan = BY_ID.get(raw) or BY_CANONICAL.get(raw)
    if not plan or plan["id"] == "enterprise":
        raise HTTPException(400, "Choose Solo, Pro, or Premium. Enterprise uses the quote form.")
    billing = "annual" if body.billing == "annual" or body.billing == "year" else "monthly"
    canonical = plan["canonical"]
    user.plan = "scale" if canonical == "agency" else canonical
    user.site_limit = int(plan["siteLimit"] or 1)
    db.add(user)
    db.commit()

    from app import site_profiles

    journey = site_profiles.get_journey(db, user.id) or {}
    state = dict(journey.get("state") or {})
    state["planId"] = canonical
    state["billing"] = billing
    try:
        site_profiles.save_journey(db, user.id, state)
    except Exception:
        # Journey save is best-effort; account plan already updated.
        pass

    _save_feature(
        db,
        user.id,
        "billing-checkout",
        "Billing checkout",
        {
            "planId": plan["id"],
            "canonical": canonical,
            "billing": billing,
            "email": (body.email or user.email or "").strip().lower(),
            "status": "trial",
            "trialDays": 14,
            "provider": "internal",
            "at": datetime.utcnow().isoformat() + "Z",
        },
    )
    return {
        "ok": True,
        "live": True,
        "plan": plan,
        "canonical": canonical,
        "billing": billing,
        "trialDays": 14,
        "next": "/app/connections",
        "detail": "14-day trial started. Payment provider checkout lands here when Stripe is connected.",
    }


@router.post("/enterprise-quote")
def enterprise_quote(
    body: EnterpriseQuoteBody,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "A work email is required.")
    sites = body.sites.strip() or "60"
    admin = _admin(db)
    owner_id = admin.id if admin else (user.id if user else 1)
    data = _feature(db, owner_id, "enterprise-quotes")
    rows = list(data.get("rows") or [])
    rows.insert(
        0,
        {
            "email": email,
            "sites": sites,
            "note": body.note.strip(),
            "fromUserId": user.id if user else None,
            "status": "pending",
            "at": datetime.utcnow().isoformat() + "Z",
        },
    )
    _save_feature(db, owner_id, "enterprise-quotes", "Enterprise quotes", {"rows": rows[:200]})
    return {
        "ok": True,
        "live": True,
        "detail": "Request sent. We'll be in touch within one business day.",
    }
