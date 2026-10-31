"""Public pricing checkout + enterprise quote inbox (no Stripe yet — plan is recorded on the account)."""

from __future__ import annotations

from datetime import datetime, timedelta
from calendar import month_abbr

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import quotas
from app.database import get_db
from app.models import CmsConnection, FeatureRecord, SiteChange, User
from app.security import get_current_user, get_optional_user

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

# Public catalog mirrors Pricing.dc.html; canonical ids match journey / quotas.
CATALOG = [
    {
        "id": "solo",
        "canonical": "starter",
        "name": "Solo",
        "tag": "ONE BUSINESS",
        "sites": "1 website",
        "siteLimit": 1,
        "mo": 49,
        "freeMonth": True,
        "founding": False,
        "popular": False,
        "features": [
            "Search Console + WordPress",
            "Colour-coded task board",
            "Up to 10 approved fixes a month",
            "Change log, one-click undo, unlimited CSV export",
            "1 branded PDF report per month",
            "2 site scans, 10 tracked keywords, and 5 backlink checks a month",
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
        "listMo": 99,
        "freeMonth": False,
        "founding": True,
        "popular": True,
        "features": [
            "Everything in Solo, with unlimited approved fixes",
            "Calls and leads from Analytics",
            "5 branded reports per site, per month",
            "1 scheduled client send + 3 client logins per site",
            "AI answer tracking and autopilot within limits",
            "8 site scans, 40 keyword checks (50 tracked), and 20 backlink checks a month",
            "Extra websites $15 USD/mo each",
        ],
    },
    {
        "id": "premium",
        "canonical": "agency",
        "name": "Agency",
        "tag": "BEST FOR AGENCIES",
        "sites": "Up to 25 websites",
        "siteLimit": 25,
        "mo": 399,
        "freeMonth": False,
        "founding": False,
        "popular": False,
        "features": [
            "Everything in Pro",
            "Unlimited white-label reports, no Searchify name",
            "Unlimited scheduled sends and client logins",
            "All-clients view and team seats",
            "30 site scans, 150 keyword checks (200 tracked), and 60 backlink checks a month",
        ],
    },
]

# Early-launch counter — wire claimed* to live billing later.
EARLY_LAUNCH = {
    "total": 100,
    "claimed": 63,
    "claimedThisWeek": 12,
    "lastClaimed": "2 hrs",
    "proListMo": 99,
    "proLaunchMo": 79,
}

BY_ID = {p["id"]: p for p in CATALOG}
BY_CANONICAL = {p["canonical"]: p for p in CATALOG}
# Stored account plans that are not catalog ids still land on a card.
STORED_TO_CATALOG = {
    "starter": "solo",
    "solo": "solo",
    "growth": "pro",
    "pro": "pro",
    "agency": "premium",
    "premium": "premium",
    "scale": "premium",
    "custom": "premium",
    "enterprise": "premium",
}


class CheckoutBody(BaseModel):
    planId: str
    billing: str = "monthly"
    email: str = ""


class EnterpriseQuoteBody(BaseModel):
    email: str
    sites: str = Field(default="60")
    note: str = ""


class CancelBody(BaseModel):
    reason: str = ""
    detail: str = ""
    note: str = ""


class RetentionBody(BaseModel):
    action: str
    reason: str = ""
    detail: str = ""
    note: str = ""


def _fmt_day(dt: datetime) -> str:
    return f"{month_abbr[dt.month]} {dt.day}"


def _fmt_joined(dt: datetime) -> str:
    return f"{month_abbr[dt.month]} {dt.day}, {dt.year}"


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _paid_through(checkout: dict, billing: str) -> datetime:
    started = _parse_iso(checkout.get("at")) or datetime.utcnow()
    days = 365 if billing == "annual" else 30
    trial = int(checkout.get("trialDays") or 0)
    if trial > 0 and (checkout.get("status") or "") == "trial":
        days = max(days, trial)
    return started + timedelta(days=days)


def _account_payload(db: Session, user: User) -> dict:
    stored = (user.plan or "starter").strip().lower()
    catalog_id = STORED_TO_CATALOG.get(stored, "solo")
    plan = BY_ID.get(catalog_id) or CATALOG[0]
    checkout = _feature(db, user.id, "billing-checkout")
    cancel = _feature(db, user.id, "billing-cancel")
    pause = _feature(db, user.id, "billing-pause")
    from app import site_profiles

    journey = site_profiles.get_journey(db, user.id) or {}
    state = journey.get("state") or {}
    ready = sum(1 for site in (state.get("sites") or []) if isinstance(site, dict) and site.get("status") == "ready")
    connected = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).count()
    billing = checkout.get("billing") or state.get("billing") or "monthly"
    if billing not in ("monthly", "annual"):
        billing = "monthly"
    status = cancel.get("status") or pause.get("status") or checkout.get("status") or "active"
    if cancel.get("status") == "cancelled":
        status = "cancelled"
    elif pause.get("status") == "paused":
        status = "paused"
    paid = _paid_through(checkout, billing)
    joined = user.created_at or datetime.utcnow()
    moves = (
        db.query(SiteChange)
        .filter(
            SiteChange.customer_id == user.id,
            SiteChange.status.in_(("shipped", "applied", "published", "closed", "done")),
        )
        .count()
    )
    biz = _feature(db, user.id, "business-profile")
    business = (
        biz.get("agency")
        or biz.get("business")
        or biz.get("name")
        or state.get("businessName")
        or (user.username or "Your workspace")
    )
    founding = bool(plan.get("founding") or checkout.get("founding"))
    price = int(plan.get("mo") or 0)
    per = "year" if billing == "annual" else "month"
    amount = price * 10 if billing == "annual" else price
    # Soft since-stats: live when we have outcomes snapshot, else empty for UI fallback.
    outcomes = _feature(db, user.id, "cancel-since") or _feature(db, user.id, "results-snapshot")
    since = outcomes.get("since") if isinstance(outcomes.get("since"), list) else []
    return {
        "ok": True,
        "live": True,
        "planId": plan["id"],
        "canonical": stored,
        "name": plan["name"],
        "billing": billing,
        "priceMo": price,
        "listMo": int(plan.get("listMo") or 0) or None,
        "amount": amount,
        "per": per,
        "founding": founding,
        "freeMonth": bool(plan.get("freeMonth")),
        "siteLimit": int(plan["siteLimit"] or 1),
        "sitesUsed": max(ready, connected),
        "allowance": quotas.summary(db, user),
        "email": user.email or "",
        "status": status,
        "trialDays": int(checkout.get("trialDays") or 0),
        "joinedAt": joined.isoformat() + "Z",
        "joinedLabel": _fmt_joined(joined),
        "paidThrough": paid.isoformat() + "Z",
        "paidThroughLabel": _fmt_day(paid),
        "movesShipped": int(moves),
        "business": str(business).strip() or "Your workspace",
        "since": since,
        "cancel": cancel or {},
        "pause": pause or {},
    }


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
    return {
        "ok": True,
        "live": True,
        "plans": CATALOG,
        "trialDays": 30,
        "earlyLaunch": EARLY_LAUNCH,
        "managed": {
            "name": "Managed",
            "tag": "DONE FOR YOU",
            "fromMo": 599,
            "href": "https://sovereignstandard.ca",
            "features": [
                "A dedicated expert SEO strategist",
                "Hands-on help with connections and onboarding",
                "Monthly progress reports",
                "Proactive monitoring to catch issues early",
                "Every feature in the Agency plan",
            ],
        },
    }


@router.get("/account")
def account(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _account_payload(db, user)


@router.post("/checkout")
def checkout(body: CheckoutBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    raw = (body.planId or "").strip().lower()
    plan = BY_ID.get(raw) or BY_CANONICAL.get(raw)
    if not plan:
        raise HTTPException(400, "Choose Solo, Pro, or Agency.")
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

    trial_days = 30 if plan.get("freeMonth") else 0
    status = "trial" if trial_days else "active"
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
            "status": status,
            "trialDays": trial_days,
            "founding": bool(plan.get("founding")),
            "provider": "internal",
            "at": datetime.utcnow().isoformat() + "Z",
        },
    )
    from app.routers.partners import referral_plan

    referral_plan(db, user, canonical)  # a referred customer's plan shows on the partner's row
    detail = (
        "First month free — card charged on day 31. Cancel any time from Settings."
        if trial_days
        else "Plan selected. Payment provider checkout lands here when Stripe is connected."
    )
    return {
        "ok": True,
        "live": True,
        "plan": plan,
        "canonical": canonical,
        "billing": billing,
        "trialDays": trial_days,
        "next": "/app/connections",
        "detail": detail,
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


@router.post("/cancel")
def cancel_subscription(body: CancelBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = _account_payload(db, user)
    if account.get("status") == "cancelled":
        return {**account, "detail": "Already cancelled.", "already": True}
    reason = (body.reason or "").strip()[:80]
    detail = (body.detail or "").strip()[:500]
    note = (body.note or "").strip()[:2000]
    payload = {
        "status": "cancelled",
        "reason": reason,
        "detail": detail,
        "note": note,
        "planId": account.get("planId"),
        "paidThrough": account.get("paidThrough"),
        "paidThroughLabel": account.get("paidThroughLabel"),
        "at": datetime.utcnow().isoformat() + "Z",
        "provider": "internal",
    }
    _save_feature(db, user.id, "billing-cancel", "Billing cancel", payload)
    # Clear pause if any
    pause = _feature(db, user.id, "billing-pause")
    if pause:
        pause = {**pause, "status": "ended", "endedAt": datetime.utcnow().isoformat() + "Z"}
        _save_feature(db, user.id, "billing-pause", "Billing pause", pause)
    # Notify admin inbox
    admin = _admin(db)
    owner_id = admin.id if admin else user.id
    inbox = _feature(db, owner_id, "cancel-feedback")
    rows = list(inbox.get("rows") or [])
    rows.insert(
        0,
        {
            "userId": user.id,
            "email": user.email or "",
            "reason": reason,
            "detail": detail,
            "note": note,
            "planId": account.get("planId"),
            "at": payload["at"],
        },
    )
    _save_feature(db, owner_id, "cancel-feedback", "Cancel feedback", {"rows": rows[:300]})
    fresh = _account_payload(db, user)
    return {
        **fresh,
        "ok": True,
        "detail": f"Cancelled. Access continues until {account.get('paidThroughLabel')}.",
    }


@router.post("/reactivate")
def reactivate(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cancel = _feature(db, user.id, "billing-cancel")
    pause = _feature(db, user.id, "billing-pause")
    if cancel:
        _save_feature(
            db,
            user.id,
            "billing-cancel",
            "Billing cancel",
            {**cancel, "status": "reactivated", "reactivatedAt": datetime.utcnow().isoformat() + "Z"},
        )
    if pause:
        _save_feature(
            db,
            user.id,
            "billing-pause",
            "Billing pause",
            {**pause, "status": "ended", "endedAt": datetime.utcnow().isoformat() + "Z"},
        )
    checkout = _feature(db, user.id, "billing-checkout")
    if checkout:
        _save_feature(
            db,
            user.id,
            "billing-checkout",
            "Billing checkout",
            {**checkout, "status": "active"},
        )
    fresh = _account_payload(db, user)
    return {
        **fresh,
        "ok": True,
        "detail": f"Welcome back. Next charge is {fresh.get('paidThroughLabel')}.",
    }


@router.post("/retention")
def retention(body: RetentionBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    action = (body.action or "").strip().lower()
    account = _account_payload(db, user)
    paid_label = account.get("paidThroughLabel") or "your next renewal"
    solo = BY_ID["solo"]
    title = ""
    message = ""

    if action in ("switch_solo", "solo", "watch_solo"):
        user.plan = solo["canonical"]
        user.site_limit = int(solo["siteLimit"] or 1)
        db.add(user)
        db.commit()
        checkout = _feature(db, user.id, "billing-checkout")
        _save_feature(
            db,
            user.id,
            "billing-checkout",
            "Billing checkout",
            {
                **(checkout or {}),
                "planId": "solo",
                "canonical": "starter",
                "status": "active",
                "at": checkout.get("at") if checkout else datetime.utcnow().isoformat() + "Z",
                "billing": account.get("billing") or "monthly",
                "founding": bool(checkout.get("founding")) if checkout else False,
            },
        )
        title = "You’re moving to Solo." if action != "watch_solo" else "You’re on watch mode."
        message = (
            f"From {paid_label} you’ll pay ${solo['mo']} a month. "
            "Pick which website to keep active before then. Nothing on your other sites is undone."
            if action != "watch_solo"
            else f"From {paid_label} you’ll pay ${solo['mo']} a month. We’ll only bring you moves that are worth your time."
        )
    elif action in ("pause", "pause_3mo"):
        _save_feature(
            db,
            user.id,
            "billing-pause",
            "Billing pause",
            {
                "status": "paused",
                "months": 3,
                "resumeAt": (datetime.utcnow() + timedelta(days=90)).isoformat() + "Z",
                "paidThroughLabel": paid_label,
                "at": datetime.utcnow().isoformat() + "Z",
            },
        )
        title = "Your plan is paused."
        message = f"No charges from {paid_label}. We’ll email you a week before it restarts, and you can restart early any time."
    elif action in ("free_month", "free"):
        checkout = _feature(db, user.id, "billing-checkout")
        _save_feature(
            db,
            user.id,
            "billing-checkout",
            "Billing checkout",
            {
                **(checkout or {}),
                "status": "trial",
                "trialDays": max(int((checkout or {}).get("trialDays") or 0), 30),
                "compMonth": True,
                "at": datetime.utcnow().isoformat() + "Z",
            },
        )
        title = "Your next month is on us."
        message = f"You won’t be charged on {paid_label}. We’ll email you each result as it comes in."
    elif action in ("autopilot", "ship_small"):
        _save_feature(
            db,
            user.id,
            "usage-controls",
            "Usage controls",
            {**_feature(db, user.id, "usage-controls"), "autonomy": "small_auto", "updatedAt": datetime.utcnow().isoformat() + "Z"},
        )
        title = "Small fixes now ship on their own."
        message = "You’ll get one Thursday email with what shipped and what still needs you. Everything stays undoable."
    elif action in ("weekly_email", "weekly"):
        _save_feature(
            db,
            user.id,
            "usage-controls",
            "Usage controls",
            {**_feature(db, user.id, "usage-controls"), "digest": "weekly", "updatedAt": datetime.utcnow().isoformat() + "Z"},
        )
        title = "Switched to one weekly email."
        message = "Thursday mornings, one email, approve from your phone."
    elif action in ("book_review", "book_founder", "report_problem"):
        admin = _admin(db)
        owner_id = admin.id if admin else user.id
        kind = {
            "book_review": "results-review-requests",
            "book_founder": "founder-call-requests",
            "report_problem": "problem-reports",
        }[action]
        inbox = _feature(db, owner_id, kind)
        rows = list(inbox.get("rows") or [])
        rows.insert(
            0,
            {
                "userId": user.id,
                "email": user.email or "",
                "reason": (body.reason or "").strip()[:80],
                "detail": (body.detail or "").strip()[:500],
                "note": (body.note or "").strip()[:2000],
                "at": datetime.utcnow().isoformat() + "Z",
            },
        )
        _save_feature(db, owner_id, kind, kind, {"rows": rows[:200]})
        if action == "report_problem":
            title, message = "We’re on it.", "Your report is at the top of our queue. You’ll hear from a real person within one business day."
        elif action == "book_founder":
            title, message = "Your call is booked.", "Check your email for the invite. Your plan stays as it is in the meantime."
        else:
            title, message = "Your review is booked.", "Check your email for the calendar invite. Your plan stays as it is until then."
    else:
        raise HTTPException(400, "Unknown retention action.")

    # Log feedback with the reason even when they stay
    if body.reason:
        admin = _admin(db)
        owner_id = admin.id if admin else user.id
        inbox = _feature(db, owner_id, "cancel-feedback")
        rows = list(inbox.get("rows") or [])
        rows.insert(
            0,
            {
                "userId": user.id,
                "email": user.email or "",
                "reason": (body.reason or "").strip()[:80],
                "detail": (body.detail or "").strip()[:500],
                "note": (body.note or "").strip()[:2000],
                "retained": action,
                "at": datetime.utcnow().isoformat() + "Z",
            },
        )
        _save_feature(db, owner_id, "cancel-feedback", "Cancel feedback", {"rows": rows[:300]})

    fresh = _account_payload(db, user)
    return {**fresh, "ok": True, "savedTitle": title, "savedBody": message, "action": action}
