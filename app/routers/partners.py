"""Partner centre — auth-gated profile, referrals, earnings, and payout details."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FeatureRecord, User
from app.security import get_current_user, hash_password

router = APIRouter(prefix="/api/v1/partners", tags=["partners"])

PRICE = {"Solo": 29, "Pro": 79, "Premium": 399}


class ApplyBody(BaseModel):
    name: str
    email: str
    channel: str = ""
    country: str = "United States"
    agreed: bool = False


class PayoutBody(BaseModel):
    method: str = "paypal"
    email: str = ""
    legalName: str = ""
    address: str = ""
    country: str = "us"
    outsideUs: bool = True
    certified: bool = False


class ResetBody(BaseModel):
    email: str


def _require_partner(user: User) -> User:
    if user.role != "ROLE_PARTNER":
        raise HTTPException(403, "Partner account required")
    return user


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


def _default_profile(user: User) -> dict:
    handle = (user.username or "partner").lower().replace(" ", "")
    return {
        "displayName": user.username or "Partner",
        "email": user.email,
        "handle": handle,
        "tier": "standard",
        "rate": 0.3,
        "since": datetime.utcnow().strftime("%b %Y"),
        "qualified": 0,
    }


def _empty_ledger() -> dict:
    return {
        "clicks": [],
        "signups": [],
        "paying": [],
        "mix": {"Solo": 0, "Pro": 0, "Premium": 0},
        "payouts": [],
        "hold": 0,
        "refs": [],
        "earnings": [],
        "refSummary": "0 sign-ups · 0 paying · 0 in free trial",
    }


def _demo_ledger() -> dict:
    """Seeded numbers for the demo partner account only — stored in DB, not the UI."""
    return {
        "clicks": [77, 100, 92, 67, 65, 93, 120, 121, 111, 122, 149, 160, 142, 123, 137, 172, 193, 185, 177, 192, 215, 216, 195, 188],
        "signups": [3, 6, 3, 3, 4, 4, 6, 7, 5, 6, 9, 7, 8, 8, 6, 10, 12, 9, 10, 13, 11, 13, 13, 10],
        "paying": [1, 2, 1, 1, 1, 2, 1, 1, 2, 2, 1, 2, 3, 2, 1, 3, 4, 1, 1, 4, 3, 1, 2, 4],
        "mix": {"Solo": 18, "Pro": 15, "Premium": 8},
        "payouts": [
            ["Jun 15, 2026", 412.4],
            ["Jul 15, 2026", 688.2],
            ["Aug 15, 2026", 894.2],
            ["Sep 15, 2026", 1118.6],
            ["Oct 15, 2026", 1302.4],
        ],
        "hold": 214.5,
        "refs": [
            ["Oct 16", "Agency · Denver, CO", "Premium", "trial"],
            ["Oct 14", "Dental clinic · Tampa, FL", "Solo", "paying"],
            ["Oct 9", "Agency · Leeds, UK", "Pro", "paying"],
            ["Oct 2", "Plumber · Austin, TX", "Solo", "paying"],
            ["Sep 27", "Agency · Toronto, ON", "Premium", "paying"],
            ["Sep 19", "Law firm · Phoenix, AZ", "Pro", "cancelled"],
            ["Sep 11", "Agency · Melbourne, AU", "Premium", "paying"],
            ["Sep 3", "Roofer · Calgary, AB", "Solo", "paying"],
        ],
        "earnings": [
            ["Oct 20", "Agency · Toronto, ON", "Premium", "hold", "Clears Nov 19"],
            ["Oct 14", "Dental clinic · Tampa, FL", "Solo", "hold", "Clears Nov 13"],
            ["Oct 9", "Agency · Leeds, UK", "Pro", "hold", "Clears Nov 8"],
            ["Oct 2", "Plumber · Austin, TX", "Solo", "cleared", "In Nov 15 payout"],
            ["Sep 30", "Agency · Melbourne, AU", "Premium", "cleared", "In Nov 15 payout"],
            ["Sep 27", "Agency · Toronto, ON", "Premium", "cleared", "In Nov 15 payout"],
            ["Sep 19", "Law firm · Phoenix, AZ", "Pro", "reversed", "Customer refunded"],
            ["Sep 3", "Roofer · Calgary, AB", "Solo", "paid", "Paid Oct 15"],
            ["Aug 30", "Agency · Melbourne, AU", "Premium", "paid", "Paid Sep 15"],
            ["Aug 27", "Agency · Toronto, ON", "Premium", "paid", "Paid Sep 15"],
        ],
        "refSummary": "186 sign-ups · 41 paying · 12 in free trial",
        "totals": {"cleared": 1469.7, "hold": 214.5, "rev": -42.6, "paid": 4415.8},
    }


def ensure_partner_account(db: Session) -> User:
    """Idempotent demo partner for partner@searchify.io / Partner-2026."""
    email = "partner@searchify.io"
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(
            username="partner",
            email=email,
            password_hash=hash_password("Partner-2026"),
            role="ROLE_PARTNER",
            plan="partner",
            site_limit=0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.role != "ROLE_PARTNER":
        user.role = "ROLE_PARTNER"
        db.add(user)
        db.commit()

    profile = _feature(db, user.id, "partner-profile")
    if not profile:
        _save_feature(
            db,
            user.id,
            "partner-profile",
            "Partner profile",
            {
                "displayName": "Sam Ortiz",
                "email": email,
                "handle": "samortiz",
                "tier": "standard",
                "rate": 0.3,
                "since": "May 2026",
                "qualified": 38,
            },
        )
    ledger = _feature(db, user.id, "partner-ledger")
    if not ledger:
        _save_feature(db, user.id, "partner-ledger", "Partner ledger", _demo_ledger())
    payout = _feature(db, user.id, "partner-payout")
    if not payout:
        _save_feature(
            db,
            user.id,
            "partner-payout",
            "Partner payout",
            {
                "method": "paypal",
                "email": "sam@ortizmedia.example",
                "legalName": "Ortiz Media LLC",
                "address": "Austin, TX",
                "country": "us",
                "outsideUs": True,
                "certified": True,
            },
        )
    return user


@router.post("/apply")
def apply(body: ApplyBody, db: Session = Depends(get_db)):
    if not body.agreed:
        raise HTTPException(400, "Agree to the Partner Program Terms to apply")
    email = body.email.strip().lower()
    name = body.name.strip()
    if not name or "@" not in email:
        raise HTTPException(400, "Name and a valid email are required")
    if db.query(User).filter((User.email == email) | (User.username == email.split("@")[0])).first():
        raise HTTPException(409, "An account already uses that email. Sign in instead.")
    # Store application on a shared inbox user (admin) or create a pending feature on a synthetic record.
    admin = db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id).first()
    owner_id = admin.id if admin else 1
    apps = _feature(db, owner_id, "partner-applications")
    rows = list(apps.get("rows") or [])
    rows.insert(
        0,
        {
            "name": name,
            "email": email,
            "channel": body.channel.strip(),
            "country": body.country.strip() or "United States",
            "status": "pending",
            "at": datetime.utcnow().isoformat() + "Z",
        },
    )
    _save_feature(db, owner_id, "partner-applications", "Partner applications", {"rows": rows[:200]})
    return {"ok": True, "detail": "Application sent. We reply within 2 business days with your partner link."}


@router.post("/reset-password")
def reset_password(body: ResetBody, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    # Never reveal whether the account exists.
    _ = db.query(User).filter(User.email == email, User.role == "ROLE_PARTNER").one_or_none()
    return {"ok": True, "detail": "If that email has a partner account, a reset link is on its way."}


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_partner(user)
    profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
    payout = _feature(db, user.id, "partner-payout")
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "profile": profile,
        "payout": payout,
        "tier": profile.get("tier") or "standard",
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_partner(user)
    profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
    ledger = {**_empty_ledger(), **_feature(db, user.id, "partner-ledger")}
    rate = float(profile.get("rate") or 0.3)
    mix = ledger.get("mix") or {"Solo": 0, "Pro": 0, "Premium": 0}
    monthly = sum(int(mix.get(k) or 0) * PRICE[k] * rate for k in PRICE)
    ref_mrr = sum(int(mix.get(k) or 0) * PRICE[k] for k in PRICE)
    paid = sum(float(p[1]) for p in (ledger.get("payouts") or []) if isinstance(p, (list, tuple)) and len(p) > 1)
    return {
        "profile": profile,
        "rate": rate,
        "tier": profile.get("tier") or "standard",
        "qualified": int(profile.get("qualified") or 0),
        "since": profile.get("since") or "this year",
        "handle": profile.get("handle") or user.username,
        "clicks": ledger.get("clicks") or [],
        "signups": ledger.get("signups") or [],
        "payingSeries": ledger.get("paying") or [],
        "mix": mix,
        "payouts": ledger.get("payouts") or [],
        "hold": float(ledger.get("hold") or 0),
        "refs": ledger.get("refs") or [],
        "refSummary": ledger.get("refSummary") or "0 sign-ups · 0 paying",
        "monthlyCommission": round(monthly, 2),
        "refMrr": round(ref_mrr, 2),
        "paidOut": round(paid, 2),
        "methodNote": (_feature(db, user.id, "partner-payout").get("method") or "paypal"),
    }


@router.get("/earnings")
def earnings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_partner(user)
    profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
    ledger = {**_empty_ledger(), **_feature(db, user.id, "partner-ledger")}
    rate = float(profile.get("rate") or 0.3)
    totals = ledger.get("totals") or {
        "cleared": 0,
        "hold": float(ledger.get("hold") or 0),
        "rev": 0,
        "paid": sum(float(p[1]) for p in (ledger.get("payouts") or []) if isinstance(p, (list, tuple)) and len(p) > 1),
    }
    return {
        "rate": rate,
        "since": profile.get("since") or "this year",
        "tier": profile.get("tier") or "standard",
        "rows": ledger.get("earnings") or [],
        "totals": totals,
        "prices": PRICE,
    }


@router.get("/payout")
def get_payout(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_partner(user)
    return {"payout": _feature(db, user.id, "partner-payout") or {}}


@router.put("/payout")
def put_payout(body: PayoutBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_partner(user)
    payload = {
        "method": body.method,
        "email": body.email.strip(),
        "legalName": body.legalName.strip(),
        "address": body.address.strip(),
        "country": body.country,
        "outsideUs": body.outsideUs,
        "certified": body.certified,
        "updatedAt": datetime.utcnow().isoformat() + "Z",
    }
    _save_feature(db, user.id, "partner-payout", "Partner payout", payload)
    return {"ok": True, "payout": payload}
