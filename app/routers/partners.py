"""Partner centre — auth-gated profile, referrals, earnings, and payout details."""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import config, mailer
from app.database import get_db
from app.models import FeatureRecord, User
from app.security import get_current_user, hash_password, make_password_token, make_token, read_password_token

log = logging.getLogger("searchify.partners")
SETUP_MINUTES = 72 * 60  # approval link: 3 days
RESET_MINUTES = 30  # reset link: 30 minutes (as the sign-in screen says)

router = APIRouter(prefix="/api/v1/partners", tags=["partners"])

PRICE = {"Solo": 29, "Pro": 79, "Premium": 399}

# The two partner programs, as the business set them. A partner's rate starts at `base` and moves to `top`
# for good once STEP_UP_REFERRALS referred customers make a first payment within STEP_UP_DAYS.
PROGRAMS = {
    "founding": {
        "key": "founding",
        "name": "Founding partner",
        "who": "Investors and developers",
        "base": 0.35,
        "top": 0.40,
    },
    "standard": {
        "key": "standard",
        "name": "Affiliate",
        "who": "Creators and affiliates on TikTok, Facebook, Instagram, YouTube, blogs and newsletters",
        "base": 0.20,
        "top": 0.25,
    },
}
STEP_UP_REFERRALS = 10
STEP_UP_DAYS = 183


def program_of(profile: dict | None) -> dict:
    return PROGRAMS["founding" if (profile or {}).get("tier") == "founding" else "standard"]


def rate_of(profile: dict | None) -> float:
    """The partner's current commission rate (the program's starting rate when none is stored)."""
    try:
        value = float((profile or {}).get("rate") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else program_of(profile)["base"]


def _recent_qualified(profile: dict, now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=STEP_UP_DAYS)
    count = 0
    for stamp in profile.get("qualifiedAt") or []:
        try:
            if datetime.fromisoformat(str(stamp).replace("Z", "")) >= cutoff:
                count += 1
        except ValueError:
            continue
    return count


def program_status(profile: dict | None, now: datetime | None = None) -> dict:
    """What the partner centre and HQ show about the partner's program and the step-up."""
    profile = profile or {}
    prog = program_of(profile)
    rate = rate_of(profile)
    recent = _recent_qualified(profile, now)
    stepped = rate >= prog["top"] - 1e-9
    return {
        **prog,
        "rate": rate,
        "qualified6m": recent,
        "need": STEP_UP_REFERRALS,
        "toGo": 0 if stepped else max(0, STEP_UP_REFERRALS - recent),
        "steppedUp": stepped,
        "rateUpAt": profile.get("rateUpAt") or "",
    }


def _count_qualified(db: Session, partner: User, paid_at: datetime) -> None:
    """A referred customer made their first payment: count it, and step the rate up when the bar is met."""
    profile = {**_default_profile(partner), **_feature(db, partner.id, "partner-profile")}
    stamps = [*(profile.get("qualifiedAt") or []), paid_at.isoformat(timespec="seconds")][-200:]
    profile["qualifiedAt"] = stamps
    profile["qualified"] = int(profile.get("qualified") or 0) + 1
    prog = program_of(profile)
    if _recent_qualified(profile, paid_at) >= STEP_UP_REFERRALS and rate_of(profile) < prog["top"]:
        profile["rate"] = prog["top"]
        profile["rateUpAt"] = paid_at.strftime("%b %d, %Y").replace(" 0", " ")
        mailer.send(
            partner.email,
            f"Your Searchify rate is now {int(round(prog['top'] * 100))}%",
            f"Hi {profile.get('displayName') or ''},\n\n{STEP_UP_REFERRALS} customers you referred started paying within "
            f"6 months, so your commission is now {int(round(prog['top'] * 100))}% on every payment from here on. "
            "It stays at this rate for good.\n",
        )
    _save_feature(db, partner.id, "partner-profile", "Partner profile", profile)


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


class SetPasswordBody(BaseModel):
    token: str
    password: str


class ClickBody(BaseModel):
    ref: str
    c: str = ""


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
        "rate": PROGRAMS["standard"]["base"],
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
        "totals": {"cleared": 0, "hold": 0, "rev": 0, "paid": 0},
        "refSummary": "0 sign-ups · 0 paying · 0 in free trial",
    }


_DEMO_LEDGER_BITS = ("Tampa", "Leeds", "Phoenix", "Calgary", "Melbourne", "Denver", "Austin, TX")


def live_ledger(stored: dict | None) -> dict:
    """Drop the seeded Sam Ortiz sample book. Rows tied to a real customer id stay."""
    ledger = {**_empty_ledger(), **(stored or {})}
    refs = [row for row in (ledger.get("refs") or []) if isinstance(row, list)]
    real_refs = [row for row in refs if len(row) > 4 and str(row[4]).startswith("c")]
    weekly = ledger.get("weekly") or {}
    blob = " ".join(str(cell) for row in refs for cell in row[:3])
    earnings = [row for row in (ledger.get("earnings") or []) if isinstance(row, list)]
    blob += " " + " ".join(str(cell) for row in earnings for cell in row[:3])
    demoish = any(bit in blob for bit in _DEMO_LEDGER_BITS) or float(ledger.get("hold") or 0) == 214.5
    if not real_refs and not weekly and demoish:
        return _empty_ledger()
    if real_refs:
        ledger["refs"] = real_refs
    if demoish:
        ledger["earnings"] = [
            row for row in earnings if not any(bit in " ".join(map(str, row[:3])) for bit in _DEMO_LEDGER_BITS)
        ]
        if not real_refs:
            ledger["payouts"] = []
            ledger["mix"] = {"Solo": 0, "Pro": 0, "Premium": 0}
            ledger["hold"] = 0
            ledger["totals"] = {"cleared": 0, "hold": 0, "rev": 0, "paid": 0}
            ledger["clicks"] = _series(weekly, "clicks") if weekly else []
            ledger["signups"] = _series(weekly, "signups") if weekly else []
            ledger["paying"] = _series(weekly, "paying") if weekly else []
    return mature(ledger)


def mature(ledger: dict, today: datetime | None = None) -> dict:
    """Commissions whose 30-day hold has passed move from 'hold' to 'cleared' (ready for the 15th)."""
    today = today or datetime.utcnow()
    totals = dict(ledger.get("totals") or {"cleared": 0, "hold": 0, "rev": 0, "paid": 0})
    rows = []
    for row in ledger.get("earnings") or []:
        row = list(row) if isinstance(row, list) else row
        # New rows: [when, who, plan, status, note, clears_iso, amount]
        if isinstance(row, list) and len(row) > 6 and row[3] == "hold":
            try:
                due = datetime.fromisoformat(str(row[5]))
            except ValueError:
                due = None
            if due and due <= today:
                amount = float(row[6] or 0)
                row[3], row[4] = "cleared", "In the next payout on the 15th"
                totals["hold"] = round(max(0.0, float(totals.get("hold") or 0) - amount), 2)
                totals["cleared"] = round(float(totals.get("cleared") or 0) + amount, 2)
        rows.append(row)
    ledger["earnings"] = rows
    ledger["totals"] = totals
    ledger["hold"] = totals.get("hold") or 0
    return ledger


def pay_out(db: Session, partner: User, *, paid_on: datetime | None = None, minimum: float = 50.0) -> dict:
    """Record a payout of the partner's cleared balance. Money is sent outside Searchify (PayPal/Wise/bank)."""
    paid_on = paid_on or datetime.utcnow()
    ledger = live_ledger(_feature(db, partner.id, "partner-ledger"))
    totals = dict(ledger.get("totals") or {})
    amount = round(float(totals.get("cleared") or 0), 2)
    if amount < minimum:
        return {"paid": 0.0, "carried": amount, "reason": f"Under the ${minimum:.0f} minimum, carried to next month."}
    label = paid_on.strftime("%b %d, %Y").replace(" 0", " ")
    for row in ledger.get("earnings") or []:
        if isinstance(row, list) and len(row) > 4 and row[3] == "cleared":
            row[3], row[4] = "paid", f"Paid {label}"
    ledger["payouts"] = [*(ledger.get("payouts") or []), [label, amount]]
    totals["paid"] = round(float(totals.get("paid") or 0) + amount, 2)
    totals["cleared"] = 0.0
    ledger["totals"] = totals
    _save_feature(db, partner.id, "partner-ledger", "Partner ledger", ledger)
    payout = _feature(db, partner.id, "partner-payout")
    return {"paid": amount, "carried": 0.0, "method": payout.get("method") or "not set", "to": payout.get("email") or partner.email}


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
                "rate": PROGRAMS["standard"]["base"],
                "since": "May 2026",
                "qualified": 38,
            },
        )
    ledger = _feature(db, user.id, "partner-ledger")
    if not ledger:
        _save_feature(db, user.id, "partner-ledger", "Partner ledger", _empty_ledger())
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
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "An account already uses that email. Sign in instead.")
    admin = db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id).first()
    owner_id = admin.id if admin else 1
    apps = _feature(db, owner_id, "partner-applications")
    rows = list(apps.get("rows") or [])
    if any(str(r.get("email") or "").lower() == email and r.get("status") == "pending" for r in rows):
        return {"ok": True, "detail": "Application sent. We reply within 2 business days with your partner link."}
    rows.insert(
        0,
        {
            # Stable id: HQ decisions are keyed by it, so new applications never shift earlier ones.
            "id": f"app-{secrets.token_hex(6)}",
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
    user = db.query(User).filter(User.email == email, User.role == "ROLE_PARTNER").one_or_none()
    if user is not None:
        link = f"{config.PUBLIC_SITE_URL}/partners/login?reset={make_password_token(user, 'reset', RESET_MINUTES)}"
        mailer.send(
            user.email,
            "Reset your Searchify partner password",
            "Someone asked to reset the password for your Searchify partner account.\n\n"
            f"Set a new password here (the link works for {RESET_MINUTES} minutes):\n{link}\n\n"
            "If you did not ask for this, ignore this email. Your password stays the same.",
        )
    # Never reveal whether the account exists.
    return {"ok": True, "detail": "If that email has a partner account, a reset link is on its way."}


@router.post("/set-password")
def set_password(body: SetPasswordBody, db: Session = Depends(get_db)):
    """Finish an approval invite or a reset: set the password and sign the partner in."""
    user = read_password_token(body.token, db)
    if user is None or user.role != "ROLE_PARTNER":
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    if len(body.password or "") < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    user.password_hash = hash_password(body.password)
    db.add(user)
    db.commit()
    return {"ok": True, "token": make_token(user), "userType": "partner", "username": user.username}


# --- Referral attribution -------------------------------------------------------------------------


def _week_key(when: datetime | None = None) -> str:
    year, week, _ = (when or datetime.utcnow()).isocalendar()
    return f"{year}-W{week:02d}"


def _series(weekly: dict, field: str, weeks: int = 24) -> list[int]:
    now = datetime.utcnow()
    keys = [_week_key(now - timedelta(weeks=offset)) for offset in range(weeks - 1, -1, -1)]
    return [int((weekly.get(key) or {}).get(field) or 0) for key in keys]


def partner_by_handle(db: Session, handle: str) -> User | None:
    want = re.sub(r"[^a-z0-9_-]", "", str(handle or "").strip().lower())
    if not want:
        return None
    for user in db.query(User).filter(User.role == "ROLE_PARTNER").all():
        profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
        if str(profile.get("handle") or "").lower() == want:
            return user
    return None


def _bump(db: Session, partner: User, field: str, ref_row: list | None = None) -> None:
    ledger = {**_empty_ledger(), **_feature(db, partner.id, "partner-ledger")}
    weekly = dict(ledger.get("weekly") or {})
    key = _week_key()
    bucket = dict(weekly.get(key) or {})
    bucket[field] = int(bucket.get(field) or 0) + 1
    weekly[key] = bucket
    ledger["weekly"] = weekly
    # The partner centre charts read these 24-week series.
    ledger["clicks"] = _series(weekly, "clicks")
    ledger["signups"] = _series(weekly, "signups")
    ledger["paying"] = _series(weekly, "paying")
    if ref_row:
        ledger["refs"] = [ref_row, *(ledger.get("refs") or [])][:200]
        total = sum(int(b.get("signups") or 0) for b in weekly.values())
        paying = sum(int(b.get("paying") or 0) for b in weekly.values())
        ledger["refSummary"] = f"{total} sign-up{'s' if total != 1 else ''} · {paying} paying"
    _save_feature(db, partner.id, "partner-ledger", "Partner ledger", ledger)


@router.post("/click")
def click(body: ClickBody, db: Session = Depends(get_db)):
    """Public: a visitor arrived through a partner link (?ref=handle). Unknown handles are ignored."""
    partner = partner_by_handle(db, body.ref)
    if partner is not None:
        _bump(db, partner, "clicks")
    return {"ok": True}


def record_signup(db: Session, ref: str, customer: User) -> bool:
    """Attribute a new customer account to the partner whose link they came through."""
    partner = partner_by_handle(db, ref)
    if partner is None or partner.id == customer.id or partner.email == customer.email:
        return False  # self-referrals never count
    when = datetime.utcnow().strftime("%b %d").replace(" 0", " ")
    # Customer names stay private: the partner sees a new customer, not who it is.
    # 5th element links the row to the customer (not shown) so plan and payments can update it.
    _bump(db, partner, "signups", [when, "New customer", "Trial", "trial", f"c{customer.id}"])
    referred = _feature(db, customer.id, "referred-by")
    if not referred:
        _save_feature(db, customer.id, "referred-by", "Referred by", {"partnerId": partner.id, "at": datetime.utcnow().isoformat() + "Z"})
    return True


PLAN_LABEL = {"solo": "Solo", "starter": "Solo", "pro": "Pro", "growth": "Pro", "agency": "Premium", "premium": "Premium", "scale": "Premium"}
HOLD_DAYS = 30
COMMISSION_MONTHS = 12


def _referrer(db: Session, customer: User) -> tuple[User | None, dict]:
    referred = _feature(db, customer.id, "referred-by")
    partner = db.get(User, int(referred["partnerId"])) if referred.get("partnerId") else None
    if partner is None or partner.role != "ROLE_PARTNER":
        return None, {}
    return partner, referred


def referral_plan(db: Session, customer: User, plan: str) -> None:
    """The referred customer chose a plan (trial). Show it on the partner's referral row."""
    partner, _ = _referrer(db, customer)
    if partner is None:
        return
    ledger = {**_empty_ledger(), **_feature(db, partner.id, "partner-ledger")}
    label = PLAN_LABEL.get(str(plan or "").lower(), str(plan or "").title() or "Trial")
    for row in ledger.get("refs") or []:
        if isinstance(row, list) and len(row) > 4 and row[4] == f"c{customer.id}":
            row[2] = label
    _save_feature(db, partner.id, "partner-ledger", "Partner ledger", ledger)


def record_payment(db: Session, customer: User, amount: float, plan: str, paid_at: datetime | None = None) -> float:
    """A referred customer paid. Adds the partner's commission to the 30-day hold.

    Call this from the payment provider's webhook (e.g. Stripe invoice.paid) once one is connected.
    Returns the commission amount (0 when the customer was not referred or is past 12 months).
    """
    partner, referred = _referrer(db, customer)
    if partner is None or amount <= 0:
        return 0.0
    paid_at = paid_at or datetime.utcnow()
    try:
        joined = datetime.fromisoformat(str(referred.get("at") or "").replace("Z", ""))
    except ValueError:
        joined = paid_at
    if paid_at - joined > timedelta(days=365 * COMMISSION_MONTHS / 12):
        return 0.0
    profile = {**_default_profile(partner), **_feature(db, partner.id, "partner-profile")}
    rate = rate_of(profile)
    commission = round(float(amount) * rate, 2)
    label = PLAN_LABEL.get(str(plan or "").lower(), str(plan or "").title())
    ledger = {**_empty_ledger(), **_feature(db, partner.id, "partner-ledger")}
    first = True
    for row in ledger.get("refs") or []:
        if isinstance(row, list) and len(row) > 4 and row[4] == f"c{customer.id}":
            first = row[3] != "paying"
            row[2], row[3] = label or row[2], "paying"
    clears = (paid_at + timedelta(days=HOLD_DAYS)).strftime("%b %d").replace(" 0", " ")
    when = paid_at.strftime("%b %d").replace(" 0", " ")
    due = (paid_at + timedelta(days=HOLD_DAYS)).isoformat(timespec="seconds")
    ledger["earnings"] = [[when, "New customer", label, "hold", f"Clears {clears}", due, commission], *(ledger.get("earnings") or [])][:500]
    totals = dict(ledger.get("totals") or {"cleared": 0, "hold": 0, "rev": 0, "paid": 0})
    totals["hold"] = round(float(totals.get("hold") or 0) + commission, 2)
    ledger["totals"] = totals
    ledger["hold"] = totals["hold"]
    if first:
        mix = dict(ledger.get("mix") or {"Solo": 0, "Pro": 0, "Premium": 0})
        if label in mix:
            mix[label] = int(mix.get(label) or 0) + 1
        ledger["mix"] = mix
    _save_feature(db, partner.id, "partner-ledger", "Partner ledger", ledger)
    if first:
        _bump(db, partner, "paying")
        _count_qualified(db, partner, paid_at)
    return commission


def record_refund(db: Session, customer: User, amount: float, plan: str) -> float:
    """Reverse a referred customer's commission when HQ records a refund."""
    partner, _referred = _referrer(db, customer)
    if partner is None or amount <= 0:
        return 0.0
    profile = {**_default_profile(partner), **_feature(db, partner.id, "partner-profile")}
    rate = rate_of(profile)
    commission = round(float(amount) * rate, 2)
    label = PLAN_LABEL.get(str(plan or "").lower(), str(plan or "").title())
    ledger = live_ledger(_feature(db, partner.id, "partner-ledger"))
    when = datetime.utcnow().strftime("%b %d").replace(" 0", " ")
    ledger["earnings"] = [[when, "New customer", label, "reversed", "Customer refunded"], *(ledger.get("earnings") or [])][:500]
    totals = dict(ledger.get("totals") or {"cleared": 0, "hold": 0, "rev": 0, "paid": 0})
    totals["rev"] = round(float(totals.get("rev") or 0) + commission, 2)
    totals["hold"] = round(max(0, float(totals.get("hold") or 0) - commission), 2)
    ledger["totals"] = totals
    ledger["hold"] = totals["hold"]
    _save_feature(db, partner.id, "partner-ledger", "Partner ledger", ledger)
    return commission


# --- HQ decisions (called from the HQ router) --------------------------------------------------------


def _unique_handle(db: Session, name: str, email: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", (name or email.split("@")[0]).lower())[:20] or "partner"
    handle, n = base, 1
    while partner_by_handle(db, handle) is not None:
        n += 1
        handle = f"{base}{n}"
    return handle


def approve_application(db: Session, owner_id: int, app_id: str, *, rate: float | None = None, founding: bool = False) -> dict:
    """Create the partner account, mark the application approved, and email a set-password link."""
    apps = _feature(db, owner_id, "partner-applications")
    rows = list(apps.get("rows") or [])
    row = next((r for r in rows if r.get("id") == app_id), None)
    if row is None:
        raise HTTPException(404, "Application not found")
    if row.get("status") == "declined":
        raise HTTPException(409, "This application was declined.")
    prog = PROGRAMS["founding" if founding else "standard"]
    rate = prog["base"] if rate is None else float(rate)
    if not prog["base"] - 1e-9 <= rate <= prog["top"] + 1e-9:
        raise HTTPException(
            400,
            f"{prog['name']} rates run from {int(round(prog['base'] * 100))}% to {int(round(prog['top'] * 100))}%.",
        )
    email = str(row.get("email") or "").lower()
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is not None and user.role != "ROLE_PARTNER":
        raise HTTPException(409, "That email already has a customer or admin account.")
    if user is None:
        username = email.split("@")[0][:40] or "partner"
        if db.query(User).filter(User.username == username).first():
            username = f"{username}-{secrets.token_hex(2)}"
        user = User(
            username=username,
            email=email,
            # Unusable until the partner sets a password from the emailed link.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role="ROLE_PARTNER",
            plan="partner",
            site_limit=0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        _save_feature(db, user.id, "partner-profile", "Partner profile", {
            "displayName": row.get("name") or user.username,
            "email": email,
            "handle": _unique_handle(db, row.get("name") or "", email),
            "tier": "founding" if founding else "standard",
            "rate": rate,
            "since": datetime.utcnow().strftime("%b %Y"),
            "qualified": 0,
            "country": row.get("country") or "",
            "channel": row.get("channel") or "",
        })
        _save_feature(db, user.id, "partner-ledger", "Partner ledger", _empty_ledger())
    profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
    setup = f"{config.PUBLIC_SITE_URL}/partners/login?setup={make_password_token(user, 'setup', SETUP_MINUTES)}"
    link = f"{config.PUBLIC_SITE_URL}/?ref={profile['handle']}"
    top_pct = int(round(prog["top"] * 100))
    rise = "" if rate >= prog["top"] else f", rising to {top_pct}% once {STEP_UP_REFERRALS} of your referrals start paying within 6 months"
    emailed = mailer.send(
        email,
        "You're approved: welcome to the Searchify partner program",
        f"Hi {row.get('name') or ''},\n\nYour Searchify partner application is approved: {prog['name']} program, "
        f"{int(round(rate * 100))}% of every payment for each customer's first 12 months{rise}.\n\n"
        f"1. Set your password (link works for 3 days):\n{setup}\n\n"
        f"2. Your referral link:\n{link}\n\n"
        "Remember to say you earn a commission when you share it.\n",
    )
    row.update({"status": "approved", "partnerId": user.id, "decidedAt": datetime.utcnow().isoformat() + "Z", "emailed": emailed})
    _save_feature(db, owner_id, "partner-applications", "Partner applications", {"rows": rows})
    return {"ok": True, "emailed": emailed, "setupUrl": setup, "referralLink": link, "partnerId": user.id, "application": row}


def decline_application(db: Session, owner_id: int, app_id: str) -> dict:
    apps = _feature(db, owner_id, "partner-applications")
    rows = list(apps.get("rows") or [])
    row = next((r for r in rows if r.get("id") == app_id), None)
    if row is None:
        raise HTTPException(404, "Application not found")
    if row.get("status") == "approved":
        raise HTTPException(409, "This application is already approved.")
    emailed = mailer.send(
        str(row.get("email") or ""),
        "Your Searchify partner application",
        f"Hi {row.get('name') or ''},\n\nThank you for applying to the Searchify partner program. "
        "We are not able to approve this application right now. You are welcome to apply again later.\n",
    )
    row.update({"status": "declined", "decidedAt": datetime.utcnow().isoformat() + "Z", "emailed": emailed})
    _save_feature(db, owner_id, "partner-applications", "Partner applications", {"rows": rows})
    return {"ok": True, "emailed": emailed, "application": row}


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
        "program": program_status(profile),
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_partner(user)
    profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
    ledger = live_ledger(_feature(db, user.id, "partner-ledger"))
    rate = rate_of(profile)
    mix = ledger.get("mix") or {"Solo": 0, "Pro": 0, "Premium": 0}
    monthly = sum(int(mix.get(k) or 0) * PRICE[k] * rate for k in PRICE)
    ref_mrr = sum(int(mix.get(k) or 0) * PRICE[k] for k in PRICE)
    paid = sum(float(p[1]) for p in (ledger.get("payouts") or []) if isinstance(p, (list, tuple)) and len(p) > 1)
    return {
        "profile": profile,
        "rate": rate,
        "tier": profile.get("tier") or "standard",
        "program": program_status(profile),
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
    ledger = live_ledger(_feature(db, user.id, "partner-ledger"))
    rate = rate_of(profile)
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


# --- Pitch writer: campaign messages for a partner, with their link and the commission disclosure -----------

WRITER_DAILY_LIMIT = 40
CHANNELS = {
    "tiktok": ("TikTok or Reels caption", 300),
    "instagram": ("Instagram post", 600),
    "facebook": ("Facebook post or group", 700),
    "youtube": ("YouTube description", 900),
    "linkedin": ("LinkedIn post", 900),
    "x": ("X / Twitter post", 270),
    "email": ("Email or newsletter", 1400),
    "blog": ("Blog or review paragraph", 1400),
    "whatsapp": ("WhatsApp or DM", 400),
}
AUDIENCES = {
    "owners": "small business owners with a WordPress site",
    "agencies": "web designers and agencies who look after client sites",
    "bloggers": "bloggers and content creators on WordPress",
    "local": "local businesses that want more customers from Google",
    "stores": "WooCommerce store owners",
}
TONES = {"friendly": "warm and plain-spoken", "expert": "knowledgeable and specific", "bold": "punchy and confident, never exaggerated"}
PRODUCT_FACTS = (
    "Searchify is an SEO tool for WordPress sites. It connects to the site, scans every page, and suggests better page titles, "
    "meta descriptions, keywords to target (from real search data) and fixes for broken or slow pages. It reads Google Search Console "
    "and Analytics so results are measured. Nothing goes live without the owner's approval, every change is checked live, and any change "
    "can be undone. Plans: Solo $29/month, Pro $79/month, Premium $399/month."
)


class WriterBody(BaseModel):
    channel: str = "instagram"
    audience: str = "owners"
    tone: str = "friendly"
    angle: str = Field(default="", max_length=300)  # what the partner wants to stress, in their words


def _disclosure(channel: str) -> str:
    if channel in ("x", "tiktok"):
        return "#ad I earn a commission if you sign up."
    return "I earn a commission if you sign up through my link, at no extra cost to you."


def _template_pitch(body: WriterBody, link: str, seed: int) -> str:
    who = AUDIENCES.get(body.audience, AUDIENCES["owners"])
    hooks = {
        "friendly": [
            "If your WordPress site isn't showing up on Google, this helped me.",
            "Quick tip for anyone running a WordPress site:",
            "I finally stopped guessing at SEO.",
        ],
        "expert": [
            "Most WordPress sites lose clicks to weak titles and descriptions.",
            "Search Console tells you what's wrong. Searchify fixes it, page by page.",
            "The fastest SEO wins are usually titles, descriptions and broken pages.",
        ],
        "bold": [
            "Stop letting your best pages hide on page 2.",
            "Your WordPress site deserves better titles than \"Home\".",
            "SEO without the guesswork.",
        ],
    }[body.tone if body.tone in TONES else "friendly"]
    middles = [
        "Searchify scans every page, writes better titles and descriptions, finds the keywords people actually search for, and nothing goes live until you approve it.",
        "It reads your real Google data, suggests fixes for each page, checks every change live, and anything can be undone in one click.",
        "It finds the pages that are close to ranking, suggests the changes, and measures the results in Search Console, with your approval at every step.",
    ]
    hook = hooks[seed % len(hooks)]
    middle = middles[(seed // 3) % len(middles)]
    angle = f" {body.angle.strip().rstrip('.')}." if body.angle.strip() else ""
    for_who = f"Made for {who}."
    disc = _disclosure(body.channel)
    if body.channel == "email":
        return (
            f"Subject: The SEO tool I use on WordPress sites\n\n{hook}\n\n{middle}{angle} {for_who}\n\n"
            f"Plans start at $29 a month: {link}\n\nHeads up: this is a partner link. {disc}"
        )
    if body.channel in ("x", "tiktok"):
        return f"{hook} {middle.split(',')[0]}.{angle} {link} {disc}"
    return f"{hook}\n\n{middle}{angle} {for_who}\n\nTry it: {link}\n\n{disc}"


def _clean_pitch(text: str, link: str, channel: str) -> str:
    text = (text or "").strip().strip('"').strip()
    # The link and the disclosure must always be there; rankings can never be promised.
    if link not in text:
        text = f"{text}\n\n{link}"
    lowered = text.lower()
    if "commission" not in lowered and "#ad" not in lowered and "affiliate" not in lowered:
        text = f"{text}\n\n{_disclosure(channel)}"
    return text


@router.post("/write-message")
def write_message(body: WriterBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Draft one campaign message for the partner. Uses AI when the server has it, otherwise our templates."""
    _require_partner(user)
    if body.channel not in CHANNELS:
        raise HTTPException(400, "Pick a channel from the list.")
    profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
    link = f"{config.MARKETING_SITE_URL.replace('https://', '').replace('http://', '')}/?ref={profile['handle']}"

    today = datetime.utcnow().strftime("%Y-%m-%d")
    usage = _feature(db, user.id, "partner-writer-usage")
    count = int(usage.get("n") or 0) if usage.get("day") == today else 0
    if count >= WRITER_DAILY_LIMIT:
        raise HTTPException(429, f"That's {WRITER_DAILY_LIMIT} drafts today. The writer opens again tomorrow.")
    _save_feature(db, user.id, "partner-writer-usage", "Partner writer usage", {"day": today, "n": count + 1})

    label, limit = CHANNELS[body.channel]
    source = "template"
    text = ""
    from app.openai_client import chat_text, openai_configured

    if openai_configured():
        system = (
            "You write short affiliate marketing messages for partners of Searchify. Facts you may use, and nothing beyond them:\n"
            f"{PRODUCT_FACTS}\n\nRules: never promise rankings, traffic or results; never invent numbers, reviews, discounts or free trials; "
            "never pretend to be Searchify; write in the first person as the partner; include the referral link exactly once; "
            "include a clear commission disclosure near the top or right next to the link (US FTC rules). "
            "Return only the message text, no quotes or notes."
        )
        prompt = (
            f"Channel: {label} (keep it under {limit} characters).\nAudience: {AUDIENCES.get(body.audience, AUDIENCES['owners'])}.\n"
            f"Tone: {TONES.get(body.tone, TONES['friendly'])}.\nReferral link: {link}\n"
            + (f"What the partner wants to stress: {body.angle.strip()}\n" if body.angle.strip() else "")
            + ("Start with a 'Subject:' line, then the email body.\n" if body.channel == "email" else "")
        )
        try:
            text, _model, _role = chat_text(system=system, user=prompt, kind="partner-pitch", max_tokens=600)
            if "guarantee" in text.lower():
                text = ""
            else:
                source = "ai"
        except Exception as exc:  # noqa: BLE001 - the template keeps the button useful when AI fails
            log.warning("partner writer AI failed: %s", exc)
            text = ""
    if not text:
        text = _template_pitch(body, link, count + user.id)
    return {
        "text": _clean_pitch(text, link, body.channel),
        "source": source,
        "channel": label,
        "left": WRITER_DAILY_LIMIT - count - 1,
    }
