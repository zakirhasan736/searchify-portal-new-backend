"""Investor / shareholder portal (invite-only). Separate from the affiliate partner centre."""

from __future__ import annotations

import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import config, mailer
from app.database import get_db
from app.models import FeatureRecord, User
from app.security import (
    get_current_user,
    hash_password,
    make_password_token,
    make_token,
    read_password_token,
    user_type,
    verify_password,
)

router = APIRouter(prefix="/api/v1/investors", tags=["investors"])

DEMO_PASSWORD = "Investor-2026"
SETUP_MINUTES = 72 * 60  # invite link: 3 days

INVESTORS = {
    "nick@petitt.example": {
        "username": "nickpetitt",
        "displayName": "Nicholas Petitt",
        "firstName": "Nick",
        "greeting": "Hey Nick,",
        "pct": 0.1,
        "pctLabel": "10%",
        "shares": "1,000,000",
        "sharesNote": "Your stake is 1,000,000 Class A shares in Aequitas Marketing Corp. Every share has the same vote and the same dividend.",
        "where": "United States",
        "taxNote": "US resident · NR301 on file · NR4 slips are issued when a dividend is paid.",
        "dividendStatus": "Declared this quarter",
        "dividendDetail": "Your share: $600.00. 15% tax withheld for CRA ($90.00). You receive $510.00 by Nov 20.",
        "dividendColor": "#3DDC84",
        "handle": "nickpetitt",
        "hasReferrals": True,
        "letterName": "Nick",
    },
    "howard@wang.example": {
        "username": "howardwang",
        "displayName": "Howard Wang",
        "firstName": "Howard",
        "greeting": "Hi Howard.",
        "pct": 0.05,
        "pctLabel": "5%",
        "shares": "500,000",
        "sharesNote": "Your stake becomes 500,000 Class A shares in Aequitas Marketing Corp. when your investment agreement is signed. Every share has the same vote and the same dividend.",
        "where": "Canada",
        "taxNote": "Canadian resident · T5 slips are sent to the address on file.",
        "dividendStatus": "Declared this quarter",
        "dividendDetail": "Your share: $300.00. No tax is withheld; you report it with your T5. Paid by Nov 20.",
        "dividendColor": "#3DDC84",
        "handle": "howardwang",
        "hasReferrals": False,
        "letterName": "Howard",
    },
}


def _feature(db: Session, user_id: int, kind: str) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict(row.payload or {}) if row else {}


def _save_feature(db: Session, user_id: int, kind: str, title: str, payload: dict) -> None:
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


def _require_investor(user: User) -> None:
    if user.role != "ROLE_INVESTOR":
        raise HTTPException(403, "Investor account required")


def ensure_investor_accounts(db: Session) -> None:
    """Seed invite-only investor demos: nick@petitt.example and howard@wang.example / Investor-2026."""
    for email, meta in INVESTORS.items():
        user = db.query(User).filter(User.email == email).one_or_none()
        if user is None:
            user = User(
                username=meta["username"],
                email=email,
                password_hash=hash_password(DEMO_PASSWORD),
                role="ROLE_INVESTOR",
                plan="investor",
                site_limit=0,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        elif user.role != "ROLE_INVESTOR":
            user.role = "ROLE_INVESTOR"
            db.add(user)
            db.commit()
        profile = _feature(db, user.id, "investor-profile")
        if not profile:
            _save_feature(
                db,
                user.id,
                "investor-profile",
                "Investor profile",
                {**meta, "email": email},
            )


class SignInBody(BaseModel):
    email: str = ""
    password: str = ""
    code: str = ""
    deviceOk: bool = False


class MessageBody(BaseModel):
    note: str = Field("", max_length=2000)


class SetPasswordBody(BaseModel):
    token: str = ""
    password: str = ""


class CreateInvestorBody(BaseModel):
    email: str = ""
    displayName: str = ""
    firstName: str = ""
    pct: float | None = None  # 0–1 ownership fraction for the portal stake page
    shares: str = ""
    where: str = ""
    taxNote: str = ""


def _unique_username(db: Session, email: str, display_name: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", (display_name or email.split("@")[0]).lower())[:28] or "investor"
    username, n = base, 1
    while db.query(User).filter(User.username == username).first() is not None:
        n += 1
        username = f"{base}{n}"[:40]
    return username


def _setup_url(user: User) -> str:
    return f"{config.PUBLIC_SITE_URL}/investors/login?setup={make_password_token(user, 'setup', SETUP_MINUTES)}"


def list_portal_investors(db: Session) -> list[dict]:
    ensure_investor_accounts(db)
    rows = []
    for user in db.query(User).filter(User.role == "ROLE_INVESTOR").order_by(User.id).all():
        profile = _profile_for(db, user)
        rows.append(
            {
                "id": f"u{user.id}",
                "userId": user.id,
                "email": user.email,
                "displayName": profile.get("displayName") or user.username,
                "pctLabel": profile.get("pctLabel") or "",
                "where": profile.get("where") or "",
                "invitedAt": profile.get("invitedAt") or None,
            }
        )
    return rows


def create_investor_account(db: Session, body: CreateInvestorBody) -> dict:
    """Owner (HQ) creates an invite-only investor login and emails a set-password link."""
    email = (body.email or "").strip().lower()
    name = " ".join((body.displayName or "").split())[:80]
    if not email or "@" not in email:
        raise HTTPException(400, "Enter a real email address.")
    if not name:
        raise HTTPException(400, "Enter the investor’s full name.")
    existing = db.query(User).filter(User.email == email).one_or_none()
    if existing is not None and existing.role != "ROLE_INVESTOR":
        raise HTTPException(409, "That email already has a customer, partner, or admin account.")
    if existing is not None:
        user = existing
    else:
        user = User(
            username=_unique_username(db, email, name),
            email=email,
            # Unusable until the investor sets a password from the emailed link.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role="ROLE_INVESTOR",
            plan="investor",
            site_limit=0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    first = " ".join((body.firstName or name.split()[0]).split())[:40] or "there"
    pct = body.pct
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            raise HTTPException(400, "Ownership must be a number between 0 and 100%.")
        if not 0 < pct <= 1:
            raise HTTPException(400, "Ownership must be above 0% and at most 100%.")
    shares = " ".join((body.shares or "").split())[:40]
    where = " ".join((body.where or "").split())[:40]
    tax_note = " ".join((body.taxNote or "").split())[:160]
    profile = {
        **(_feature(db, user.id, "investor-profile") or {}),
        "email": email,
        "displayName": name,
        "firstName": first,
        "letterName": first,
        "greeting": f"Hey {first},",
        "handle": user.username,
        "where": where,
        "taxNote": tax_note,
        "shares": shares,
        "invitedAt": datetime.utcnow().isoformat() + "Z",
        "invitedBy": "hq",
    }
    if pct is not None:
        profile["pct"] = round(pct, 6)
        profile["pctLabel"] = f"{round(pct * 10000) / 100:g}%"
        if shares:
            profile["sharesNote"] = (
                f"Your stake is {shares} Class A shares in Aequitas Marketing Corp. "
                "Every share has the same vote and the same dividend."
            )
    _save_feature(db, user.id, "investor-profile", "Investor profile", profile)

    setup = _setup_url(user)
    emailed = mailer.send(
        email,
        "Your Searchify investor portal invite",
        f"Hi {first},\n\n"
        "Aadle created your Searchify investor account. There is no public sign-up — this invite is the only way in.\n\n"
        f"1. Set your password (link works for 3 days):\n{setup}\n\n"
        f"2. Then sign in at:\n{config.PUBLIC_SITE_URL}/investors/login\n\n"
        "If you did not expect this, reply to Aadle and ignore the link.\n",
    )
    return {
        "ok": True,
        "emailed": emailed,
        "setupUrl": setup,
        "investor": {
            "id": f"u{user.id}",
            "userId": user.id,
            "email": email,
            "displayName": name,
            "pctLabel": profile.get("pctLabel") or "",
        },
        "detail": (
            "Invite emailed with a set-password link."
            if emailed
            else "Account created. Email isn’t set up — copy the set-password link and send it yourself."
        ),
    }


def resend_investor_invite(db: Session, investor_key: str) -> dict:
    try:
        user = db.get(User, int(str(investor_key).lstrip("u")))
    except (TypeError, ValueError):
        user = None
    if user is None or user.role != "ROLE_INVESTOR":
        raise HTTPException(404, "Investor account not found")
    profile = _profile_for(db, user)
    first = profile.get("firstName") or profile.get("letterName") or "there"
    setup = _setup_url(user)
    emailed = mailer.send(
        user.email,
        "Set your Searchify investor password",
        f"Hi {first},\n\n"
        f"Set your investor portal password here (works for 3 days):\n{setup}\n\n"
        f"Then sign in at {config.PUBLIC_SITE_URL}/investors/login\n",
    )
    return {
        "ok": True,
        "emailed": emailed,
        "setupUrl": setup,
        "detail": "Invite resent." if emailed else "Email isn’t set up — copy the link below.",
    }


def _profile_for(db: Session, user: User) -> dict:
    stored = _feature(db, user.id, "investor-profile")
    email = (user.email or "").lower()
    base = dict(INVESTORS.get(email) or {})
    return {**base, **stored, "email": email, "displayName": stored.get("displayName") or base.get("displayName") or user.username}


def _stake_payload(profile: dict) -> dict:
    pct = profile.get("pctLabel") or "5%"
    return {
        "ok": True,
        "live": False,
        "sample": True,
        "displayName": profile.get("displayName") or "Investor",
        "firstName": profile.get("firstName") or "there",
        "greeting": profile.get("greeting") or f"Hi {profile.get('firstName') or 'there'}.",
        "headline": f"You own {pct} of Searchify.",
        "sharesNote": profile.get("sharesNote")
        or "Class A shares in Aequitas Marketing Corp. Every share has the same vote and the same dividend.",
        "dividendStatus": profile.get("dividendStatus") or "Not declared yet",
        "dividendDetail": profile.get("dividendDetail") or "Dividends are declared when the agreement tests are met.",
        "dividendColor": profile.get("dividendColor") or "#9AA1AB",
        "rules": [
            {
                "title": "Six profitable months in a row",
                "detail": "April to September were all profitable.",
                "ok": True,
            },
            {
                "title": "Three months of expenses in the bank",
                "detail": "The reserve is fully covered before any dividend.",
                "ok": True,
            },
            {
                "title": "Legal solvency test",
                "detail": "Confirmed by the company’s accountant for this dividend.",
                "ok": True,
            },
        ],
        "quarter": {
            "label": "How the company did · July to September",
            "from": "From the Q3 update, sent Oct 20",
            "kpis": [
                {"label": "Revenue", "value": "$71,240", "sub": "for the quarter", "tone": "#9AA1AB"},
                {"label": "Profit after tax provision", "value": "$12,560", "sub": "up each month", "tone": "#3DDC84"},
                {"label": "Paying customers", "value": "131", "sub": "+19 this quarter", "tone": "#3DDC84"},
                {"label": "Monthly churn", "value": "2.1%", "sub": "target under 3%", "tone": "#3DDC84"},
            ],
            "noteTitle": "A note from Aadle",
            "note": "Strong quarter: agencies are now a third of revenue. Next quarter we focus on lowering the undo rate below 3%.",
        },
        "dividends": [
            {
                "quarter": "Q3 2026",
                "meta": "Declared Oct 28 · pays by Nov 20",
                "share": "$300.00" if profile.get("pct", 0) <= 0.05 else "$600.00",
                "withheld": "—" if (profile.get("where") or "").lower().startswith("canada") else "$90.00",
                "receive": "$300.00" if profile.get("pct", 0) <= 0.05 else "$510.00",
            }
        ],
        "dividendsNote": "You’ll get a T5 slip by the end of February to report the dividend on your Canadian return."
        if (profile.get("where") or "").lower().startswith("canada")
        else "You’ll get an NR4 slip for the withheld amount. Keep it for your US return.",
        "documents": [
            {"title": "Your shareholder agreement", "meta": "Signed 2021 · PDF", "href": "#"},
            {"title": "Your share certificate", "meta": "Issued when your agreement is signed", "href": "#"},
            {"title": "Q3 2026 shareholder update", "meta": "Sent Oct 20", "href": "#"},
            {"title": "Q2 2026 shareholder update", "meta": "Sent Jul 22", "href": "#"},
            {"title": "T5 slips" if (profile.get("where") or "").lower().startswith("canada") else "NR4 slips", "meta": "Issued each year a dividend is paid", "href": "#"},
        ],
        "taxNote": profile.get("taxNote") or "",
        "contact": {
            "title": "Questions?",
            "body": "Message Aadle directly. Replies within two business days.",
            "cta": "Send a message",
        },
    }


def _earnings_payload(profile: dict) -> dict:
    handle = profile.get("handle") or "investor"
    if not profile.get("hasReferrals"):
        return {
            "ok": True,
            "live": False,
            "sample": True,
            "displayName": profile.get("displayName"),
            "headlineHtml": '<span style="color:#9FE7F5">$0.00</span> cleared. No referrals in the hold yet.',
            "sub": "Founding partners earn commission on referrals separately from dividends. Your referral link is ready when you want it.",
            "kpis": [
                {"label": "Cleared", "value": "$0.00", "sub": "nothing due yet", "color": "#9FE7F5"},
                {"label": "In the 30-day hold", "value": "$0.00", "sub": "no open holds", "color": "#F2F4F5"},
                {"label": "Reversed this month", "value": "$0.00", "sub": "no refunds", "color": "#FFC53D"},
                {"label": "Paid out so far", "value": "$0.00", "sub": "start sharing your link", "color": "#F2F4F5"},
            ],
            "rate": 35,
            "rateBlurb": "of every payment on Solo, Pro and Agency, each customer's first 12 months",
            "managedRate": 30,
            "managedBlurb": "on the Managed SEO plan. Our team does that work by hand, so more labour goes into every client.",
            "rateNote": "Locked in your agreement; it can't be lowered without your written consent. Commissions are a company expense, so they're paid before profit is calculated and come out before any dividend.",
            "link": f"searchify.io/?ref={handle}",
            "linkStats": [
                {"label": "Clicks", "value": "0", "sub": "last 6 months"},
                {"label": "Sign-ups", "value": "0", "sub": "—"},
                {"label": "Paying", "value": "0", "sub": "—"},
                {"label": "Qualified", "value": "0", "sub": "past the 30-day hold"},
            ],
            "ledger": [],
            "ledgerNote": "Commissions are fees for your referral work, not dividends. They're reported on a separate slip, and you report them on your own tax return.",
        }
    return {
        "ok": True,
        "live": False,
        "sample": True,
        "displayName": profile.get("displayName"),
        "headlineHtml": '<span style="color:#9FE7F5">$296.45</span> cleared. $313.95 still in the 30-day hold.',
        "sub": "Your commission rate on referrals, from the Founding Partner Agreement. Paid separately from any dividends on your stake.",
        "kpis": [
            {"label": "Cleared", "value": "$296.45", "sub": "paying on Nov 15", "color": "#9FE7F5"},
            {"label": "In the 30-day hold", "value": "$313.95", "sub": "clears over the next 4 weeks", "color": "#F2F4F5"},
            {"label": "Reversed this month", "value": "−$17.15", "sub": "1 refund", "color": "#FFC53D"},
            {"label": "Paid out so far", "value": "$1,541.25", "sub": "since Jul 2026", "color": "#F2F4F5"},
        ],
        "rate": 35,
        "rateBlurb": "of every payment on Solo, Pro and Agency, each customer's first 12 months",
        "managedRate": 30,
        "managedBlurb": "on the Managed SEO plan. Our team does that work by hand, so more labour goes into every client.",
        "rateNote": "Locked in your agreement; it can't be lowered without your written consent. Commissions are a company expense, so they're paid before profit is calculated and come out before any dividend.",
        "link": f"searchify.io/?ref={handle}",
        "linkStats": [
            {"label": "Clicks", "value": "412", "sub": "last 6 months"},
            {"label": "Sign-ups", "value": "27", "sub": "6.6% of clicks"},
            {"label": "Paying", "value": "9", "sub": "33% of sign-ups"},
            {"label": "Qualified", "value": "8", "sub": "past the 30-day hold"},
        ],
        "ledger": [
            {"date": "Oct 19", "customer": "Agency · Austin, TX", "plan": "Agency", "payment": "$399.00", "earn": "$139.65", "earnColor": "#9FE7F5", "status": "In 30-day hold", "statusColor": "#4DA3FF", "statusMeta": "Clears Nov 18"},
            {"date": "Oct 16", "customer": "Med spa · Irvine, CA", "plan": "Pro", "payment": "$99.00", "earn": "$34.65", "earnColor": "#9FE7F5", "status": "In 30-day hold", "statusColor": "#4DA3FF", "statusMeta": "Clears Nov 15"},
            {"date": "Oct 12", "customer": "Agency · Denver, CO", "plan": "Agency", "payment": "$399.00", "earn": "$139.65", "earnColor": "#9FE7F5", "status": "In 30-day hold", "statusColor": "#4DA3FF", "statusMeta": "Clears Nov 11"},
            {"date": "Oct 8", "customer": "Gym · Pasadena, CA", "plan": "Solo", "payment": "$49.00", "earn": "$17.15", "earnColor": "#9FE7F5", "status": "Cleared", "statusColor": "#3DDC84", "statusMeta": "In Nov 15 payout"},
            {"date": "Oct 2", "customer": "Agency · Fresno, CA", "plan": "Agency", "payment": "$399.00", "earn": "$139.65", "earnColor": "#9FE7F5", "status": "Cleared", "statusColor": "#3DDC84", "statusMeta": "In Nov 15 payout"},
            {"date": "Sep 29", "customer": "Agency · Austin, TX", "plan": "Agency", "payment": "$399.00", "earn": "$139.65", "earnColor": "#9FE7F5", "status": "Cleared", "statusColor": "#3DDC84", "statusMeta": "In Nov 15 payout"},
            {"date": "Sep 21", "customer": "Café · Burbank, CA", "plan": "Solo", "payment": "$49.00", "earn": "−$17.15", "earnColor": "#FFC53D", "status": "Reversed", "statusColor": "#FFC53D", "statusMeta": "Customer refunded"},
            {"date": "Sep 19", "customer": "Agency · Denver, CO", "plan": "Agency", "payment": "$399.00", "earn": "$139.65", "earnColor": "#9AA1AB", "status": "Paid", "statusColor": "#9AA1AB", "statusMeta": "Paid Oct 15"},
        ],
        "ledgerNote": "Commissions are fees for your referral work, not dividends. They're reported on a separate slip, and you report them on your own tax return.",
    }


def _playbook_payload(profile: dict) -> dict:
    first = profile.get("letterName") or profile.get("firstName") or "friend"
    return {
        "ok": True,
        "displayName": profile.get("displayName"),
        "greeting": profile.get("greeting") or f"Hey {first},",
        "letterName": first,
        "mediaKitHref": "#",
    }


@router.post("/signin")
def signin(body: SignInBody, db: Session = Depends(get_db)):
    ensure_investor_accounts(db)
    email = body.email.strip().lower()
    user = db.query(User).filter(User.email == email, User.role == "ROLE_INVESTOR").one_or_none()
    if user is None or not verify_password(body.password, user.password_hash or ""):
        raise HTTPException(401, "That email and password don't match an investor account.")
    code = (body.code or "").strip()
    if not code and not body.deviceOk:
        return {"ok": True, "step": "code", "detail": "Enter the 6-digit code from your authenticator app."}
    if code and code not in {"123456", "000000"}:
        raise HTTPException(401, "That two-step code isn't right. Try again.")
    if code and not body.deviceOk:
        return {"ok": True, "step": "device", "detail": "Confirm this device to finish signing in."}
    profile = _profile_for(db, user)
    return {
        "ok": True,
        "step": "done",
        "token": make_token(user),
        "userType": user_type(user.role),
        "username": user.username,
        "displayName": profile.get("displayName"),
    }


@router.post("/set-password")
def set_password(body: SetPasswordBody, db: Session = Depends(get_db)):
    """Finish an HQ invite: set the password and sign the investor in."""
    user = read_password_token(body.token, db)
    if user is None or user.role != "ROLE_INVESTOR":
        raise HTTPException(400, "This link has expired or was already used. Ask Aadle for a new one.")
    if len(body.password or "") < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    user.password_hash = hash_password(body.password)
    db.add(user)
    db.commit()
    profile = _profile_for(db, user)
    return {
        "ok": True,
        "token": make_token(user),
        "userType": "investor",
        "username": user.username,
        "displayName": profile.get("displayName"),
    }


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_investor(user)
    profile = _profile_for(db, user)
    return {"ok": True, "userType": "investor", **profile}


@router.get("/stake")
def stake(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_investor(user)
    return _stake_payload(_profile_for(db, user))


@router.get("/earnings")
def earnings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_investor(user)
    return _earnings_payload(_profile_for(db, user))


@router.get("/playbook")
def playbook(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_investor(user)
    return _playbook_payload(_profile_for(db, user))


@router.post("/message")
def message(body: MessageBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_investor(user)
    note = (body.note or "").strip()
    if len(note) < 3:
        raise HTTPException(400, "Write a short message first.")
    inbox = _feature(db, user.id, "investor-messages")
    rows = list(inbox.get("rows") or [])
    rows.insert(0, {"note": note[:2000], "at": __import__("datetime").datetime.utcnow().isoformat() + "Z"})
    _save_feature(db, user.id, "investor-messages", "Investor messages", {"rows": rows[:50]})
    return {"ok": True, "message": "Sent to Aadle. Expect a reply within two business days."}
