"""HQ (admin) — overview metrics and customer account desk."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app import hq_live
from app.models import FeatureRecord, User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/hq", tags=["hq"])

PRICE = {"solo": 29, "pro": 79, "premium": 399, "ent": 1250}


class CustomerActionBody(BaseModel):
    action: str = Field(..., description="record_payment|mark_failed|free_month|refund|change_plan|cancel|impersonate")
    plan: str | None = None
    amount: float | None = None  # record_payment: amount paid (defaults to the plan price)
    reason: str = "Customer asked"


class PayoutRunBody(BaseModel):
    ids: list[str] | None = None  # partner ids ("u12"); default: every partner approved in the run


class PartnersStateBody(BaseModel):
    run: dict | None = None
    paid: bool | None = None
    apps: dict | None = None
    rates: dict | None = None
    programs: dict | None = None  # {"u12": "founding" | "standard"}
    cleared: dict | None = None
    grants: dict | None = None


class ProblemActionBody(BaseModel):
    action: str = Field(..., description="resolve|email")


class ShareholdersStateBody(BaseModel):
    scenario: str | None = None
    amount: float | None = None
    solvent: bool | None = None
    prepared: bool | None = None
    quarterly: str | None = None
    howard: list | None = None
    holders: list[dict] | None = None  # cap table: [{name, pct 0-1, shares, where, wh 0-0.5, note}]


def _require_admin(user: User) -> User:
    if user.role != "ROLE_ADMIN":
        raise HTTPException(403, "Admin account required")
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


def _admin_owner(db: Session) -> User:
    admin = db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id).first()
    if admin is None:
        raise HTTPException(500, "No admin account")
    return admin


def _demo_overview() -> dict:
    return {
        "months": ["Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct"],
        "planCounts": [
            [6, 3, 1, 0],
            [10, 5, 2, 0],
            [15, 8, 3, 0],
            [19, 11, 5, 0],
            [24, 15, 7, 1],
            [29, 19, 10, 1],
            [34, 24, 13, 1],
            [38, 29, 17, 2],
            [44, 34, 21, 2],
            [49, 39, 25, 3],
            [54, 44, 28, 3],
            [58, 49, 31, 4],
        ],
        "prices": PRICE,
        "funnels": {
            "30": [12400, 1860, 212, 123, 16],
            "90": [34100, 5050, 581, 331, 57],
            "365": [118000, 17400, 1980, 1130, 189],
        },
        "health": {
            "30": [96, "3.8%", "69%", "7 min"],
            "90": [262, "4.1%", "67%", "8 min"],
            "365": [804, "4.6%", "64%", "11 min"],
        },
        "churn": "2.1%",
        "churnNote": "3 cancellations · target under 3%",
        "affiliates": [
            {"initials": "SO", "name": "Sam Ortiz", "sub": "186 sign-ups · 41 paying", "owed": "$1,469.70"},
            {"initials": "WP", "name": "WP Agency Collective", "sub": "74 sign-ups · 17 paying", "owed": "$842.10"},
            {"initials": "LM", "name": "Local Marketing Lab", "sub": "52 sign-ups · 9 paying", "owed": "$318.00"},
            {"initials": "DK", "name": "Dana Kim", "sub": "21 sign-ups · 4 paying", "owed": "$64.20"},
        ],
        "problemsCount": 9,
    }


def _demo_customers() -> dict:
    return {
        "total": 131,
        "items": [
            {
                "id": "rivera",
                "name": "Rivera Digital",
                "owner": "Ana Rivera",
                "email": "ana@riveradigital.example",
                "domain": "riveradigital.example",
                "plan": "premium",
                "status": "paying",
                "since": "Mar 2026",
                "ref": "Sam Ortiz (partner)",
                "card": "Visa ending 4412",
                "next": "Nov 3",
                "moves": "412",
                "undo": "2.9% undone",
                "ltv": 2793,
                "sites": [
                    ["riveradigital.example", 1, 1, 1],
                    ["bayside-dental.example", 1, 1, 1],
                    ["northline-roofing.example", 1, 0, 1],
                ],
                "log": [],
            },
            {
                "id": "tampa",
                "name": "Bright Smile Dental",
                "owner": "Dr. Leah Moss",
                "email": "office@brightsmile.example",
                "domain": "brightsmile.example",
                "plan": "solo",
                "status": "paying",
                "since": "Oct 2026",
                "ref": "Sam Ortiz (partner)",
                "card": "Mastercard ending 0931",
                "next": "Nov 14",
                "moves": "9",
                "undo": "0 undone",
                "ltv": 29,
                "sites": [["brightsmile.example", 1, 1, 1]],
                "log": [],
            },
            {
                "id": "leeds",
                "name": "Wharf Street Agency",
                "owner": "Tom Hale",
                "email": "tom@wharfstreet.example",
                "domain": "wharfstreet.example",
                "plan": "pro",
                "status": "failed",
                "since": "Jul 2026",
                "ref": "Direct",
                "card": "Visa ending 2200 · declined",
                "next": "Retry Oct 23",
                "moves": "138",
                "undo": "4.1% undone",
                "ltv": 237,
                "sites": [
                    ["wharfstreet.example", 1, 1, 1],
                    ["kirkgate-cafe.example", 0, 1, 1],
                ],
                "log": [],
            },
            {
                "id": "austin",
                "name": "Lone Star Plumbing",
                "owner": "Jay Ruiz",
                "email": "jay@lonestarplumb.example",
                "domain": "lonestarplumb.example",
                "plan": "solo",
                "status": "paying",
                "since": "Oct 2026",
                "ref": "Sam Ortiz (partner)",
                "card": "Amex ending 1007",
                "next": "Nov 2",
                "moves": "14",
                "undo": "0 undone",
                "ltv": 29,
                "sites": [["lonestarplumb.example", 1, 1, 1]],
                "log": [],
            },
            {
                "id": "toronto",
                "name": "Maple & Main Media",
                "owner": "Priya Shah",
                "email": "priya@maplemain.example",
                "domain": "maplemain.example",
                "plan": "premium",
                "status": "trial",
                "since": "Oct 2026",
                "ref": "WP Agency Collective",
                "card": "No card yet",
                "next": "Trial ends Oct 30",
                "moves": "22",
                "undo": "0 undone",
                "ltv": 0,
                "sites": [["maplemain.example", 1, 1, 0]],
                "log": [],
            },
            {
                "id": "phoenix",
                "name": "Desert Law Group",
                "owner": "Mark Ellis",
                "email": "mellis@desertlaw.example",
                "domain": "desertlaw.example",
                "plan": "pro",
                "status": "cancelled",
                "since": "Sep 2026",
                "ref": "Sam Ortiz (partner)",
                "card": "Visa ending 7781",
                "next": "None",
                "moves": "31",
                "undo": "6.5% undone",
                "ltv": 79,
                "sites": [["desertlaw.example", 1, 1, 1]],
                "log": [],
            },
            {
                "id": "calgary",
                "name": "Summit Roofing",
                "owner": "Dale Kim",
                "email": "dale@summitroof.example",
                "domain": "summitroof.example",
                "plan": "solo",
                "status": "paying",
                "since": "Sep 2026",
                "ref": "Local Marketing Lab",
                "card": "Visa ending 5520",
                "next": "Nov 3",
                "moves": "40",
                "undo": "2.5% undone",
                "ltv": 58,
                "sites": [["summitroof.example", 1, 1, 1]],
                "log": [],
            },
            {
                "id": "melb",
                "name": "Southbank Creative",
                "owner": "Ella Ng",
                "email": "ella@southbank.example",
                "domain": "southbank.example",
                "plan": "premium",
                "status": "paying",
                "since": "Sep 2026",
                "ref": "Sam Ortiz (partner)",
                "card": "Visa ending 8890",
                "next": "Nov 11",
                "moves": "96",
                "undo": "3.1% undone",
                "ltv": 798,
                "sites": [
                    ["southbank.example", 1, 1, 1],
                    ["yarra-physio.example", 1, 1, 1],
                ],
                "log": [],
            },
        ],
    }


def _demo_partners() -> dict:
    return {
        "run": [
            {"id": "sam", "name": "Sam Ortiz", "type": "Partner · 30%", "cleared": 1512.3, "rev": -42.6, "method": "PayPal", "tax": True, "flag": ""},
            {"id": "wp", "name": "WP Agency Collective", "type": "Partner · 30%", "cleared": 861.3, "rev": -19.2, "method": "Wise · GBP", "tax": True, "flag": ""},
            {"id": "lml", "name": "Local Marketing Lab", "type": "Partner · 30%", "cleared": 318.0, "rev": 0, "method": "Bank · CAD", "tax": False, "flag": ""},
            {"id": "nick", "name": "Nicholas Petitt", "type": "Founding partner · 35%", "cleared": 961.45, "rev": 0, "method": "PayPal", "tax": True, "flag": ""},
            {"id": "emin", "name": "Emin Navasartian", "type": "Founding partner · 35%", "cleared": 96.95, "rev": 0, "method": "Wise · USD", "tax": True, "flag": ""},
            {"id": "dana", "name": "Dana Kim", "type": "Partner · 30%", "cleared": 64.2, "rev": 0, "method": "PayPal", "tax": True, "flag": "Possible self-referral"},
            {"id": "howard", "name": "Howard Wang", "type": "Founding partner · 35%", "cleared": 41.65, "rev": 0, "method": "Bank · CAD", "tax": True, "flag": ""},
        ],
        "applications": [
            {
                "id": "a1",
                "name": "Marisol Vega",
                "email": "marisol@wpstudio.example",
                "country": "Mexico",
                "when": "Oct 18",
                "where": "YouTube channel on WordPress for small businesses, 42k subscribers",
                "note": "Looks like a strong fit.",
                "noteColor": "#3DDC84",
            },
            {
                "id": "a2",
                "name": "Ben Carter",
                "email": "ben@bestdeals.example",
                "country": "United States",
                "when": "Oct 17",
                "where": "Coupon and discount code website",
                "note": "Coupon sites tend to claim sales that would have happened anyway.",
                "noteColor": "#FFC53D",
            },
            {
                "id": "a3",
                "name": "Grace Liu",
                "email": "grace@growthlocal.example",
                "country": "Canada",
                "when": "Oct 15",
                "where": "Local SEO newsletter, 6,800 readers, plus client referrals",
                "note": "Looks like a strong fit.",
                "noteColor": "#3DDC84",
            },
        ],
        "partners": [
            {"id": "sam", "name": "Sam Ortiz", "type": "Affiliate since May 2026", "rate": 20, "founding": False, "qual": 38, "paying": 41, "life": 5779.7, "flag": ""},
            {"id": "wp", "name": "WP Agency Collective", "type": "Affiliate since Jun 2026", "rate": 20, "founding": False, "qual": 15, "paying": 17, "life": 2104.9, "flag": ""},
            {"id": "lml", "name": "Local Marketing Lab", "type": "Affiliate since Jul 2026", "rate": 20, "founding": False, "qual": 8, "paying": 9, "life": 744.0, "flag": "Tax details missing"},
            {"id": "dana", "name": "Dana Kim", "type": "Affiliate since Aug 2026", "rate": 20, "founding": False, "qual": 4, "paying": 4, "life": 128.4, "flag": "2 sign-ups used Dana's card"},
            {"id": "nick", "name": "Nicholas Petitt", "type": "Founding partner", "rate": 35, "founding": True, "qual": 8, "paying": 13, "life": 2502.7, "flag": ""},
            {"id": "emin", "name": "Emin Navasartian", "type": "Founding partner", "rate": 35, "founding": True, "qual": 3, "paying": 4, "life": 188.1, "flag": ""},
            {"id": "howard", "name": "Howard Wang", "type": "Founding partner", "rate": 35, "founding": True, "qual": 2, "paying": 2, "life": 83.3, "flag": ""},
        ],
        "founders": [
            {"id": "nick", "name": "Nicholas Petitt", "qual": 8, "mrr": 2747, "held": 3},
            {"id": "emin", "name": "Emin Navasartian", "qual": 3, "mrr": 1210, "held": 0},
            {"id": "howard", "name": "Howard Wang", "qual": 2, "mrr": 540, "held": 0},
        ],
        "steps": [2500, 5000, 10000, 20000, 40000],
        "state": {"run": {}, "paid": False, "apps": {}, "rates": {}, "cleared": {}, "grants": {}},
    }


def _demo_problems() -> dict:
    return {
        "items": [
            {
                "id": "cb1",
                "cat": "chargeback",
                "sev": "URGENT",
                "title": "Chargeback opened: $399.00",
                "who": "Southbank Creative",
                "since": "2 days ago · respond by Oct 27",
                "detail": "The bank says the charge wasn't recognized. They've used Searchify daily (96 moves shipped), so the evidence is strong. The partner commission on this payment is on hold until it's decided.",
                "primaryLabel": "Send evidence pack",
                "doneText": "Evidence sent to Stripe: invoice, usage log and sign-up record.",
            },
            {
                "id": "pf1",
                "cat": "payment",
                "sev": "HIGH",
                "title": "Card declined: $79.00 Pro renewal",
                "who": "Wharf Street Agency",
                "since": "today · retry 2 of 4 on Oct 23",
                "detail": "Visa ending 2200, insufficient funds. They've had 2 reminder emails. Access continues until day 14.",
                "primaryLabel": "Retry the charge now",
                "doneText": "Charge retried: $79.00 paid.",
            },
            {
                "id": "pf2",
                "cat": "payment",
                "sev": "HIGH",
                "title": "Card expired: $29.00 Solo renewal",
                "who": "Kirkgate Café",
                "since": "4 days ago · retry 3 of 4 on Oct 24",
                "detail": "Mastercard ending 3391 expired in September. A new card is needed; retrying will fail.",
                "primaryLabel": "Send update-card link",
                "doneText": "Secure update-card link sent.",
            },
            {
                "id": "cn1",
                "cat": "connection",
                "sev": "HIGH",
                "title": "WordPress connection broken",
                "who": "Rivera Digital · northline-roofing.example",
                "since": "6 hours ago",
                "detail": "The site changed its login plugin and our access key stopped working. 4 approved moves are waiting to ship.",
                "primaryLabel": "Send reconnect steps",
                "doneText": "Reconnect steps sent. The moves ship once the key works.",
            },
            {
                "id": "cn2",
                "cat": "connection",
                "sev": "MEDIUM",
                "title": "Search Console access removed",
                "who": "Wharf Street Agency · kirkgate-cafe.example",
                "since": "1 day ago",
                "detail": "Their Google account removed Searchify. Rankings and clicks stop updating until it's reconnected.",
                "primaryLabel": "Send reconnect steps",
                "doneText": "Reconnect steps sent.",
            },
            {
                "id": "jb1",
                "cat": "job",
                "sev": "MEDIUM",
                "title": "Weekly scan stuck for 3 hours",
                "who": "Maple & Main Media",
                "since": "3 hours ago",
                "detail": "The scan stopped at page 214 of 380. The site started returning slow responses, likely a hosting limit.",
                "primaryLabel": "Restart at a slower pace",
                "doneText": "Restarted at half speed. Finishes in about 40 minutes.",
            },
            {
                "id": "jb2",
                "cat": "job",
                "sev": "LOW",
                "title": "2 drafted moves failed to save",
                "who": "Bright Smile Dental",
                "since": "yesterday",
                "detail": "Their page builder locked the pages while drafts were saving. Nothing changed on the live site.",
                "primaryLabel": "Try again",
                "doneText": "Saved. Both moves are back in their approval queue.",
            },
            {
                "id": "rb1",
                "cat": "rollback",
                "sev": "MEDIUM",
                "title": "Change rolled back automatically",
                "who": "Summit Roofing · /roof-repair",
                "since": "yesterday",
                "detail": "A title change was followed by a 38% drop in clicks over 5 days, so Searchify restored the old title. Worth a look before a similar move ships elsewhere.",
                "primaryLabel": "Mark as reviewed",
                "doneText": "Reviewed. Added to the learning notes for title changes.",
            },
            {
                "id": "rb2",
                "cat": "rollback",
                "sev": "LOW",
                "title": "Customer undid 3 moves in a row",
                "who": "Desert Law Group",
                "since": "3 days ago",
                "detail": 'They undid three internal-link moves, then cancelled. The reason given: "links went to the wrong practice areas."',
                "primaryLabel": "Mark as reviewed",
                "doneText": "Reviewed and tagged for the product team.",
            },
        ],
        "done": {},
        "emailed": {},
    }


def _demo_shareholders() -> dict:
    return {
        "holders": [
            {"name": "Aadle Mohammad Faqiryar", "pct": 0.8, "shares": "8,000,000", "where": "Canada", "wh": 0, "note": "Canada · T5 slip", "noteColor": "#7D848E", "color": "#D5F879"},
            {"name": "Nicholas Petitt", "pct": 0.1, "shares": "1,000,000", "where": "United States", "wh": 0.15, "note": "US · NR301 on file · NR4 slip", "noteColor": "#7D848E", "color": "#9FE7F5"},
            {"name": "Emin Navasartian", "pct": 0.05, "shares": "500,000", "where": "United States", "wh": 0.25, "note": "US · NR301 missing: 25% until it arrives", "noteColor": "#FFC53D", "color": "#4DA3FF"},
            {"name": "Howard Wang", "pct": 0.05, "shares": "500,000", "where": "Canada", "wh": 0, "note": "Canada · T5 slip", "noteColor": "#7D848E", "color": "#8A93A3"},
        ],
        "scenarios": {
            "sample": {"m": [["Apr", 1840], ["May", 2310], ["Jun", 2950], ["Jul", 3420], ["Aug", 4180], ["Sep", 4960]], "cash": 58400, "exp": 14200},
            "today": {"m": [["Apr", -2350], ["May", -1980], ["Jun", -2600], ["Jul", -2140], ["Aug", -2410], ["Sep", -2200]], "cash": 3200, "exp": 2250},
        },
        "state": {
            "scenario": "sample",
            "amount": 6000,
            "solvent": False,
            "prepared": False,
            "quarterly": "none",
            "howard": [],
        },
    }


def ensure_hq_demo(db: Session) -> None:
    """Kept so older startup code can call it. HQ no longer stores sample books."""
    _ = db


@router.get("/overview")
def overview(
    range: str = Query("30", pattern="^(30|90|365)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    rows = hq_live.customer_list(db)
    problems = _open_problems(db)
    payload = hq_live.overview(db, rows, len(problems))
    return {"ok": True, "live": True, "range": range, **payload}


@router.get("/customers")
def list_customers(
    q: str = "",
    status: str = "all",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    items = hq_live.customer_list(db)
    qq = q.strip().lower()
    if status and status != "all":
        items = [c for c in items if c.get("status") == status]
    if qq:
        items = [
            c
            for c in items
            if qq
            in f"{c.get('name','')} {c.get('email','')} {c.get('domain','')} {c.get('owner','')}".lower()
        ]
    return {"ok": True, "live": True, "total": len(items), "items": items}


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    for row in hq_live.customer_list(db):
        if row.get("id") == customer_id:
            return {"ok": True, "live": True, "customer": row}
    raise HTTPException(404, "Customer not found")


@router.put("/customers/{customer_id}")
def act_customer(
    customer_id: str,
    body: CustomerActionBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    result = hq_live.apply_customer_action(
        db,
        customer_id,
        body.action,
        body.plan,
        body.reason or "Customer asked",
        user.username or "Admin",
        body.amount,
    )
    if result.get("error") == "Unknown plan":
        raise HTTPException(400, "Unknown plan")
    if result.get("error"):
        raise HTTPException(400, result["error"])
    if not result.get("customer"):
        raise HTTPException(404, "Customer not found")
    return {"ok": True, "message": result["message"], "customer": result["customer"]}


def _open_problems(db: Session) -> list[dict]:
    admin = _admin_owner(db)
    state = _feature(db, admin.id, "hq-problems-state")
    done = state.get("done") or {}
    return [item for item in hq_live.problem_items(db) if not done.get(item.get("id"))]


def _partners_payload(db: Session, admin: User) -> dict:
    merged = hq_live.partner_book(db)
    stored = _feature(db, admin.id, "hq-partners-state")
    state = {**(merged.get("state") or {}), **stored}
    # Applications are the real inbox from the partner apply form (never sample rows).
    inbox = _feature(db, admin.id, "partner-applications")
    rows = list(inbox.get("rows") or [])
    changed = False
    for row in rows:
        if not row.get("id"):
            # Older rows were keyed by list position; give them a stable id once.
            import secrets

            row["id"] = f"app-{secrets.token_hex(6)}"
            changed = True
    if changed:
        _save_feature(db, admin.id, "partner-applications", "Partner applications", {"rows": rows})
    decided = {"approved": "ok", "declined": "no"}
    merged["applications"] = [
        {
            "id": row["id"],
            "name": row.get("name") or "",
            "email": row.get("email") or "",
            "country": row.get("country") or "",
            "when": (row.get("at") or "")[:10] or "Recent",
            "where": row.get("channel") or "(not given)",
            "note": "",
            "noteColor": "#9AA1AB",
            "status": row.get("status") or "pending",
            "emailed": row.get("emailed"),
            "partnerId": f"u{row['partnerId']}" if row.get("partnerId") else "",
        }
        for row in rows
    ]
    # Decisions come from the application record, not from a UI flag.
    state["apps"] = {row["id"]: decided[row["status"]] for row in rows if row.get("status") in decided}
    merged["state"] = state
    return merged


def _real_partners(db: Session) -> list[dict]:
    """Partner accounts that exist in the database (approved applications and the seeded partner)."""
    from app.routers.partners import PRICE, _default_profile, program_status

    out = []
    for user in db.query(User).filter(User.role == "ROLE_PARTNER").order_by(User.id).all():
        profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
        ledger = _feature(db, user.id, "partner-ledger")
        weekly = ledger.get("weekly") or {}
        paying = sum(int(b.get("paying") or 0) for b in weekly.values()) or sum(int(v or 0) for v in (ledger.get("mix") or {}).values())
        life = sum(float(p[1]) for p in (ledger.get("payouts") or []) if isinstance(p, (list, tuple)) and len(p) > 1)
        founding = profile.get("tier") == "founding"
        out.append({
            "id": f"u{user.id}",
            "name": profile.get("displayName") or user.username,
            "email": user.email,
            "handle": profile.get("handle") or "",
            "type": ("Founding partner" if founding else "Affiliate") + f" since {profile.get('since') or ''}".rstrip(),
            "rate": int(round(rate_of(profile) * 100)),
            "founding": founding,
            "program": program_status(profile),
            "qual": int(profile.get("qualified") or 0),
            "paying": paying,
            "life": round(life, 2),
            "flag": "",
            "real": True,
        })
    return out


from app.routers.partners import PROGRAMS, program_of, rate_of  # noqa: E402


class DecisionBody(BaseModel):
    rate: float | None = None  # percent; default is the program's starting rate
    founding: bool = False  # True: founding program (investors, developers). False: affiliate program


@router.post("/partners/applications/{app_id}/approve")
def approve_partner(app_id: str, body: DecisionBody | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Create the partner account and send the set-password link. Returns the link when email is not set up."""
    _require_admin(user)
    from app.routers.partners import approve_application

    body = body or DecisionBody()
    rate = None if body.rate is None else float(body.rate) / 100
    return approve_application(db, _admin_owner(db).id, app_id, rate=rate, founding=bool(body.founding))


@router.post("/partners/applications/{app_id}/decline")
def decline_partner(app_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app.routers.partners import decline_application

    return decline_application(db, _admin_owner(db).id, app_id)


@router.post("/partners/{partner_key}/invite")
def resend_invite(partner_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """New set-password link for an approved partner (lost email, expired link)."""
    _require_admin(user)
    from app import config, mailer
    from app.routers.partners import SETUP_MINUTES
    from app.security import make_password_token

    try:
        partner = db.get(User, int(partner_key.lstrip("u")))
    except ValueError:
        partner = None
    if partner is None or partner.role != "ROLE_PARTNER":
        raise HTTPException(404, "Partner not found")
    setup = f"{config.PUBLIC_SITE_URL}/partners/login?setup={make_password_token(partner, 'setup', SETUP_MINUTES)}"
    emailed = mailer.send(partner.email, "Set your Searchify partner password", f"Set your password here (works for 3 days):\n{setup}\n")
    return {"ok": True, "emailed": emailed, "setupUrl": setup}


@router.get("/partners")
def get_partners(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    data = _partners_payload(db, admin)
    return {"ok": True, "live": True, **data}


@router.put("/partners")
def put_partners(body: PartnersStateBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    state = dict(_feature(db, admin.id, "hq-partners-state") or {})
    if body.run is not None:
        state["run"] = {**state.get("run", {}), **body.run}
    if body.paid is not None:
        state["paid"] = body.paid
    if body.apps is not None:
        state["apps"] = {**state.get("apps", {}), **body.apps}
    if body.rates is not None:
        state["rates"] = {**state.get("rates", {}), **body.rates}
        # The rate lives on the partner's own account, so the partner centre shows the same number.
        from app.routers.partners import _save_feature as save_partner_feature

        for key, value in body.rates.items():
            try:
                partner = db.get(User, int(str(key).lstrip("u")))
                pct = float(value)
            except (TypeError, ValueError):
                continue
            if partner is None or partner.role != "ROLE_PARTNER":
                continue
            profile = _feature(db, partner.id, "partner-profile")
            prog = program_of(profile)
            if not round(prog["base"] * 100, 4) <= pct <= round(prog["top"] * 100, 4):
                raise HTTPException(
                    400,
                    f"{prog['name']} rates run from {int(round(prog['base'] * 100))}% to {int(round(prog['top'] * 100))}%.",
                )
            if profile.get("tier") == "founding" and pct / 100 < rate_of(profile):
                raise HTTPException(400, "A founding partner's rate can only be moved up.")
            profile["rate"] = round(pct / 100, 4)
            save_partner_feature(db, partner.id, "partner-profile", "Partner profile", profile)
    if body.programs is not None:
        # Move a partner between programs: the rate moves to the new program's starting rate
        # (or stays, when it is already inside the new range).
        from app.routers.partners import _save_feature as save_partner_feature

        for key, value in body.programs.items():
            if value not in PROGRAMS:
                raise HTTPException(400, "Program must be founding or standard.")
            try:
                partner = db.get(User, int(str(key).lstrip("u")))
            except (TypeError, ValueError):
                continue
            if partner is None or partner.role != "ROLE_PARTNER":
                continue
            profile = _feature(db, partner.id, "partner-profile")
            new = PROGRAMS[value]
            current = rate_of(profile)
            profile["tier"] = value
            profile["rate"] = current if new["base"] <= current <= new["top"] else new["base"]
            save_partner_feature(db, partner.id, "partner-profile", "Partner profile", profile)
    if body.cleared is not None:
        state["cleared"] = {**state.get("cleared", {}), **body.cleared}
    if body.grants is not None:
        state["grants"] = {**state.get("grants", {}), **body.grants}
    _save_feature(db, admin.id, "hq-partners-state", "HQ partners state", state)
    data = _partners_payload(db, admin)
    return {"ok": True, "live": True, **data}


@router.post("/partners/payout")
def run_partner_payout(body: PayoutRunBody | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Record this month's payouts: each approved partner's cleared balance moves to Paid.

    The money itself is sent outside Searchify (PayPal, Wise or bank) using the method on file.
    """
    _require_admin(user)
    from app.routers.partners import _feature as partner_feature
    from app.routers.partners import pay_out

    admin = _admin_owner(db)
    state = dict(_feature(db, admin.id, "hq-partners-state") or {})
    approved = [key for key, value in (state.get("run") or {}).items() if value == "approved"]
    ids = (body.ids if body and body.ids else approved) or []
    results, total = [], 0.0
    for key in ids:
        try:
            partner = db.get(User, int(str(key).lstrip("u")))
        except ValueError:
            partner = None
        if partner is None or partner.role != "ROLE_PARTNER":
            continue
        payout = partner_feature(db, partner.id, "partner-payout")
        if not (payout.get("certified") or payout.get("legalName")):
            results.append({"id": key, "paid": 0, "held": True, "reason": "Tax details missing."})
            continue
        done = pay_out(db, partner)
        total += done["paid"]
        results.append({"id": key, **done})
    state["paid"] = bool(total)
    state["lastRun"] = {"at": datetime.utcnow().isoformat() + "Z", "total": round(total, 2), "results": results}
    state["run"] = {k: v for k, v in (state.get("run") or {}).items() if k not in {r["id"] for r in results if r.get("paid")}}
    _save_feature(db, admin.id, "hq-partners-state", "HQ partners state", state)
    data = _partners_payload(db, admin)
    paid_n = len([r for r in results if r.get("paid")])
    message = (
        f"Recorded ${total:,.2f} for {paid_n} partner{'s' if paid_n != 1 else ''}. Send each amount yourself by PayPal, Wise, or bank."
        if paid_n
        else "Nothing was paid: no approved partner had a cleared balance over the minimum with tax details on file."
    )
    return {"ok": True, "message": message, "results": results, **data}


def _demo_cancellations() -> dict:
    """Sample cancel desk when no live cancel-feedback rows exist yet."""
    return {
        "headline": "7 tried to cancel in October. 4 stayed.",
        "sub": "Every reason, every comment and every offer, from the customer's cancel flow. Read them all; this is where the product gets better.",
        "kpis": [
            {"label": "Started to cancel", "value": "7", "sub": "October so far", "color": "#F2F4F5"},
            {"label": "Stayed", "value": "4", "sub": "3 took an offer, 1 paused", "color": "#3DDC84"},
            {"label": "Cancelled", "value": "3", "sub": "matches 2.1% monthly churn", "color": "#FF8A80"},
            {"label": "Revenue lost", "value": "$137", "sub": "a month, from those 3", "color": "#FF8A80"},
            {"label": "Revenue kept", "value": "$507", "sub": "a month, plus $29 paused", "color": "#3DDC84"},
        ],
        "reasons": [
            {"label": "Costs too much", "stayed": 4, "left": 2, "total": 6, "keptPct": 67},
            {"label": "Not seeing results", "stayed": 3, "left": 2, "total": 5, "keptPct": 60},
            {"label": "Got what I needed", "stayed": 1, "left": 3, "total": 4, "keptPct": 25},
            {"label": "Reviewing takes too long", "stayed": 2, "left": 1, "total": 3, "keptPct": 67},
            {"label": "Missing something", "stayed": 0, "left": 3, "total": 3, "keptPct": 0},
            {"label": "Something isn’t working", "stayed": 1, "left": 1, "total": 2, "keptPct": 50},
            {"label": "Switching tools", "stayed": 0, "left": 2, "total": 2, "keptPct": 0},
            {"label": "Business closing", "stayed": 0, "left": 1, "total": 1, "keptPct": 0},
            {"label": "Something else", "stayed": 0, "left": 1, "total": 1, "keptPct": 0},
        ],
        "reasonsNote": "“Missing something” and “Switching tools” keep nobody yet. That's a product problem, not an offer problem: see the requests on the right.",
        "offers": [
            {"label": "Switch to Solo", "shown": 8, "taken": 3, "rate": 38},
            {"label": "Take next month free", "shown": 5, "taken": 3, "rate": 60},
            {"label": "Pause up to 3 months", "shown": 7, "taken": 2, "rate": 29},
            {"label": "Ship small fixes on its own", "shown": 3, "taken": 2, "rate": 67},
            {"label": "Fix it first", "shown": 2, "taken": 1, "rate": 50},
            {"label": "15-minute review call", "shown": 6, "taken": 1, "rate": 17},
        ],
        "comments": [
            {
                "id": "desert",
                "outcome": "left",
                "needsReply": True,
                "name": "Desert Law Group",
                "meta": "Pro · since Sep · 31 moves shipped",
                "when": "Oct 21",
                "reason": "Something isn’t working · “links went to the wrong practice areas”",
                "quote": "Three of the internal links pointed family-law pages at criminal-defence pages. I undid them, but I stopped trusting it after that.",
                "note": "Offered “fix it first”. Declined. Referred by Sam Ortiz: his commission stops after Nov 21.",
                "toProduct": True,
                "customerId": "",
                "email": "hello@desertlaw.example",
            },
            {
                "id": "harbour",
                "outcome": "left",
                "needsReply": True,
                "name": "Harbour Yoga Studio",
                "meta": "Solo · 2 months · 9 moves shipped",
                "when": "Oct 17",
                "reason": "I’m moving to another tool · “Squarespace”",
                "quote": "We’re moving the site off WordPress to Squarespace and it sounds like you don’t support it.",
                "note": "No offer shown. Platform request counted.",
                "toProduct": True,
                "customerId": "",
                "email": "hi@harbouryoga.example",
            },
            {
                "id": "oakridge",
                "outcome": "left",
                "needsReply": True,
                "name": "Oakridge Plumbing",
                "meta": "Solo · 6 months · 44 moves shipped",
                "when": "Oct 9",
                "reason": "I got what I needed",
                "quote": "Honestly it did its job. We’re ranking for everything we wanted.",
                "note": "Offered watch mode on Solo and a pause. Declined both. Good win-back candidate in 60 days.",
                "toProduct": False,
                "customerId": "",
                "email": "office@oakridge.example",
            },
            {
                "id": "bright",
                "outcome": "stayed",
                "needsReply": False,
                "name": "Bright Smile Dental",
                "meta": "Pro · 1 month · 18 moves shipped",
                "when": "Oct 24",
                "reason": "I’m not seeing results",
                "quote": "Nothing has moved yet and I’m paying for it.",
                "note": "Took the free month. 11 changes still being measured, first results due Nov 3.",
                "toProduct": False,
                "customerId": "tampa",
                "email": "office@brightsmile.example",
            },
        ],
        "requests": [
            {"label": "Squarespace or Wix support", "count": 3},
            {"label": "Shopify support", "count": 2},
            {"label": "Smarter internal links (match by topic)", "count": 2},
            {"label": "Bulk approve across client sites", "count": 2},
            {"label": "White-label client reports", "count": 1},
        ],
        "followUps": [
            {"when": "Right away", "text": "Confirmation email: the date access ends, what stays on their site, how to come back."},
            {"when": "Day 2", "text": "If they wrote a comment: a short personal reply from the founder. Not a template."},
            {"when": "3 days left", "text": "Heads-up that access ends soon, and that their launch price goes with it."},
            {"when": "Day 60", "text": "Win-back email, only if something they asked for has shipped or their reason was “got what I needed”."},
            {"when": "Paused", "text": "Email a week before billing restarts, with one-click “pause a bit longer” or “cancel”."},
        ],
        "houseRule": "One reason screen, at most two offers, and “continue cancelling” always one tap away. No hiding the button, no making people email or call to leave.",
        "openCount": 4,
        "problemsCount": 9,
    }


def _live_cancellations(db: Session) -> dict | None:
    """Build cancel desk from admin cancel-feedback inbox when real rows exist."""
    admin = _admin_owner(db)
    inbox = _feature(db, admin.id, "cancel-feedback")
    rows = list(inbox.get("rows") or [])
    if not rows:
        return None
    customers = {str(c.get("id")): c for c in hq_live.customer_list(db)}
    by_email = {str(c.get("email") or "").lower(): c for c in customers.values()}
    comments = []
    reason_bucket: dict[str, dict] = {}
    left = stayed = 0
    lost = kept = 0.0
    for row in rows[:80]:
        email = str(row.get("email") or "").lower()
        cust = by_email.get(email) or {}
        outcome = "left"
        plan = str(row.get("planId") or cust.get("plan") or "solo").lower()
        price = float(PRICE.get(plan, 29) or 29)
        # Reactivated / paused feedback stays; cancelled stays left.
        status = str(row.get("status") or "cancelled").lower()
        if status in {"reactivated", "paused", "stayed"}:
            outcome = "stayed"
            stayed += 1
            kept += price
        else:
            left += 1
            lost += price
        reason = str(row.get("reason") or "Something else").strip() or "Something else"
        bucket = reason_bucket.setdefault(reason, {"stayed": 0, "left": 0})
        bucket["stayed" if outcome == "stayed" else "left"] += 1
        when = str(row.get("at") or "")[:10]
        comments.append(
            {
                "id": f"c{row.get('userId') or email}-{when}",
                "outcome": outcome,
                "needsReply": outcome == "left" and bool(row.get("note") or row.get("detail")),
                "name": cust.get("name") or email or "Customer",
                "meta": f"{plan.title()} · {cust.get('since') or 'recent'}",
                "when": when or "—",
                "reason": reason + (f" · “{row.get('detail')}”" if row.get("detail") else ""),
                "quote": str(row.get("note") or row.get("detail") or "No comment left."),
                "note": "From live cancel flow.",
                "toProduct": "missing" in reason.lower() or "squarespace" in str(row.get("detail") or "").lower(),
                "customerId": cust.get("id") or "",
                "email": email,
            }
        )
    reasons = []
    for label, counts in reason_bucket.items():
        total = counts["stayed"] + counts["left"]
        kept_pct = int(round(100 * counts["stayed"] / total)) if total else 0
        reasons.append(
            {
                "label": label,
                "stayed": counts["stayed"],
                "left": counts["left"],
                "total": total,
                "keptPct": kept_pct,
            }
        )
    reasons.sort(key=lambda r: (-r["total"], r["label"]))
    started = left + stayed
    demo = _demo_cancellations()
    return {
        **demo,
        "live": True,
        "headline": f"{started} started to cancel. {stayed} stayed.",
        "sub": "Live from the customer cancel flow.",
        "kpis": [
            {"label": "Started to cancel", "value": str(started), "sub": "from cancel feedback", "color": "#F2F4F5"},
            {"label": "Stayed", "value": str(stayed), "sub": "reactivated or paused", "color": "#3DDC84"},
            {"label": "Cancelled", "value": str(left), "sub": "access ending at period end", "color": "#FF8A80"},
            {"label": "Revenue lost", "value": f"${int(round(lost))}", "sub": "a month, from those who left", "color": "#FF8A80"},
            {"label": "Revenue kept", "value": f"${int(round(kept))}", "sub": "a month, from those who stayed", "color": "#3DDC84"},
        ],
        "reasons": reasons or demo["reasons"],
        "comments": comments or demo["comments"],
        "openCount": sum(1 for c in comments if c.get("needsReply")),
    }


@router.get("/cancellations")
def get_cancellations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    live = _live_cancellations(db)
    if live:
        return {"ok": True, "live": True, **live}
    return {"ok": True, "live": False, **_demo_cancellations()}


class CancelDeskActionBody(BaseModel):
    action: str = Field(..., description="reply|product|done")
    email: str = ""
    note: str = ""


@router.put("/cancellations/{item_id}")
def act_cancellation(
    item_id: str,
    body: CancelDeskActionBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Track HQ follow-up on a cancel comment (reply / send to product / mark done)."""
    _require_admin(user)
    admin = _admin_owner(db)
    state = _feature(db, admin.id, "hq-cancel-desk")
    done = dict(state.get("done") or {})
    product = dict(state.get("product") or {})
    replied = dict(state.get("replied") or {})
    if body.action == "done":
        done[item_id] = True
        msg = "Marked done."
    elif body.action == "product":
        product[item_id] = True
        msg = "Sent to product."
    elif body.action == "reply":
        from app import mailer

        to = (body.email or "").strip()
        if to and mailer.send(to, "A note from Searchify", (body.note or "Thanks for the feedback — we're on it.").strip()):
            replied[item_id] = True
            msg = f"Reply emailed to {to}."
        else:
            replied[item_id] = True
            msg = "Reply logged. Email isn’t set up, so contact them directly."
    else:
        raise HTTPException(400, "Unknown action")
    _save_feature(db, admin.id, "hq-cancel-desk", "HQ cancel desk", {"done": done, "product": product, "replied": replied})
    return {"ok": True, "message": msg, "done": done, "product": product, "replied": replied}


@router.get("/problems")
def get_problems(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    state = _feature(db, admin.id, "hq-problems-state")
    items = hq_live.problem_items(db)
    done = state.get("done") or {}
    emailed = state.get("emailed") or {}
    open_n = len([item for item in items if not done.get(item.get("id"))])
    return {"ok": True, "live": True, "openCount": open_n, "items": items, "done": done, "emailed": emailed}


@router.put("/problems/{problem_id}")
def act_problem(
    problem_id: str,
    body: ProblemActionBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    items = hq_live.problem_items(db)
    if problem_id not in {item.get("id") for item in items}:
        raise HTTPException(404, "Problem not found")
    state = _feature(db, admin.id, "hq-problems-state")
    done = dict(state.get("done") or {})
    emailed = dict(state.get("emailed") or {})
    if body.action == "resolve":
        done[problem_id] = True
        msg = "Resolved."
    elif body.action == "email":
        from app import mailer

        item = next(item for item in items if item.get("id") == problem_id)
        to = item.get("email") or ""
        if not to:
            raise HTTPException(400, "This problem has no customer email.")
        if mailer.send(to, item.get("emailSubject") or "A note from Searchify", item.get("emailBody") or ""):
            emailed[problem_id] = True
            msg = f"Email sent to {to}."
        else:
            # Never claim an email went out when it did not.
            msg = f"Email isn't set up, so nothing was sent. Contact {to} directly."
    else:
        raise HTTPException(400, "Unknown action")
    _save_feature(db, admin.id, "hq-problems-state", "HQ problems state", {"done": done, "emailed": emailed})
    open_n = len([item for item in items if not done.get(item.get("id"))])
    return {"ok": True, "message": msg, "openCount": open_n, "done": done, "emailed": emailed}


@router.get("/shareholders")
def get_shareholders(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    rows = hq_live.customer_list(db)
    data = hq_live.shareholders(db, rows)
    stored = _feature(db, admin.id, "hq-shareholders-state")
    if stored:
        data["state"] = {**data["state"], **stored}
        if stored.get("holders"):
            data["holders"] = stored["holders"]
    from app.routers.investors import list_portal_investors

    data["portalInvestors"] = list_portal_investors(db)
    return {"ok": True, "live": True, **data}


class CreateInvestorHqBody(BaseModel):
    email: str = ""
    displayName: str = ""
    firstName: str = ""
    pctPercent: float | None = None  # 0–100 from the HQ form
    shares: str = ""
    where: str = ""
    taxNote: str = ""


@router.get("/investors")
def get_investors(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app.routers.investors import list_portal_investors

    return {"ok": True, "live": True, "portalInvestors": list_portal_investors(db)}


@router.post("/investors")
def create_investor(body: CreateInvestorHqBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Aadle/HQ creates an invite-only investor login and emails the set-password link."""
    _require_admin(user)
    from app.routers.investors import CreateInvestorBody, create_investor_account, list_portal_investors

    pct = None
    if body.pctPercent is not None:
        try:
            pct = float(body.pctPercent) / 100.0
        except (TypeError, ValueError):
            raise HTTPException(400, "Ownership % must be a number.")
    result = create_investor_account(
        db,
        CreateInvestorBody(
            email=body.email,
            displayName=body.displayName,
            firstName=body.firstName,
            pct=pct,
            shares=body.shares,
            where=body.where,
            taxNote=body.taxNote,
        ),
    )
    result["portalInvestors"] = list_portal_investors(db)
    return result


@router.post("/investors/{investor_key}/invite")
def resend_investor_invite_hq(investor_key: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Resend the set-password link when the investor lost the email or the link expired."""
    _require_admin(user)
    from app.routers.investors import resend_investor_invite

    return resend_investor_invite(db, investor_key)


@router.put("/shareholders")
def put_shareholders(
    body: ShareholdersStateBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    state = dict(_feature(db, admin.id, "hq-shareholders-state") or {})
    if body.scenario is not None:
        state["scenario"] = body.scenario
    if body.amount is not None:
        state["amount"] = body.amount
    if body.solvent is not None:
        state["solvent"] = body.solvent
    if body.prepared is not None:
        state["prepared"] = body.prepared
    if body.quarterly is not None:
        state["quarterly"] = body.quarterly
    if body.howard is not None:
        state["howard"] = body.howard
    if body.holders is not None:
        clean = []
        for row in body.holders[:50]:
            name = " ".join(str(row.get("name") or "").split())[:80]
            if not name:
                continue
            try:
                pct = float(row.get("pct") or 0)
                wh = float(row.get("wh") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, f"Check the numbers for {name}.")
            if not 0 < pct <= 1 or not 0 <= wh <= 0.5:
                raise HTTPException(400, f"{name}: ownership must be above 0% and at most 100%, withholding 0–50%.")
            clean.append({
                "name": name,
                "pct": round(pct, 6),
                "shares": " ".join(str(row.get("shares") or "").split())[:30],
                "where": " ".join(str(row.get("where") or "").split())[:40],
                "wh": round(wh, 4),
                "note": " ".join(str(row.get("note") or "").split())[:120],
            })
        if sum(r["pct"] for r in clean) > 1.0001:
            raise HTTPException(400, "Ownership adds up to more than 100%.")
        state["holders"] = clean
    _save_feature(db, admin.id, "hq-shareholders-state", "HQ shareholders state", state)
    rows = hq_live.customer_list(db)
    data = hq_live.shareholders(db, rows)
    data["state"] = {**data["state"], **state}
    data["holders"] = state.get("holders") or []
    return {"ok": True, "live": True, **data}


@router.get("/money")
def get_money(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """System owner: Stripe balance, MRR, affiliate liability, dividends still to pay."""
    _require_admin(user)
    from app import stripe_payments

    return stripe_payments.money_snapshot(db)


class DividendDeclareBody(BaseModel):
    pool: float = 0
    note: str = ""


class DividendPaidBody(BaseModel):
    reference: str = ""


@router.post("/dividends/declare")
def declare_dividend(body: DividendDeclareBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import stripe_payments

    return stripe_payments.declare_dividend(db, pool=float(body.pool or 0), note=body.note or "")


@router.post("/dividends/{run_id}/approve")
def approve_dividend(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import stripe_payments

    return stripe_payments.approve_dividend(db, run_id)


@router.post("/dividends/{run_id}/paid")
def mark_dividend_paid(run_id: str, body: DividendPaidBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Record that the approved dividend was sent by bank or Wise. No Stripe Connect."""
    _require_admin(user)
    from app import stripe_payments

    return stripe_payments.mark_dividend_paid(db, run_id, body.reference or "")


class PayoutSettingsBody(BaseModel):
    affiliateSolo: float | None = None
    affiliatePro: float | None = None
    affiliateAgency: float | None = None
    affiliateManaged: float | None = None
    partnerSolo: float | None = None
    partnerPro: float | None = None
    partnerAgency: float | None = None
    partnerManaged: float | None = None
    affiliateMonths: int | None = None
    partnerMonths: int | None = None
    holdDays: int | None = None
    minimumPayout: float | None = None
    reserveRate: float | None = None
    cashCoverMonths: int | None = None
    profitableMonthsRequired: int | None = None
    owners: list[dict] | None = None


class StatementBody(BaseModel):
    month: str = ""
    note: str = ""
    bankBalance: float | None = None
    revenueGross: float | None = None
    stripeFees: float | None = None
    refunds: float | None = None
    salesTax: float | None = None
    costs: dict | None = None


@router.get("/payout-settings")
def get_payout_settings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    return {"ok": True, "settings": payout_policy.load_settings(db)}


@router.put("/payout-settings")
def put_payout_settings(body: PayoutSettingsBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    return {"ok": True, "settings": payout_policy.save_settings(db, body.model_dump(exclude_none=True))}


@router.get("/statements")
def get_statements(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    return {"ok": True, "statements": payout_policy.list_statements(db)}


@router.post("/statements")
def draft_statement(body: StatementBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    statement = payout_policy.build_statement(
        db,
        (body.month or datetime.utcnow().strftime("%Y-%m")).strip(),
        costs=body.costs,
        bank_balance=body.bankBalance,
        note=body.note,
        revenue={
            "revenueGross": body.revenueGross,
            "stripeFees": body.stripeFees,
            "refunds": body.refunds,
            "salesTax": body.salesTax,
        },
    )
    return {"ok": True, "statement": payout_policy.upsert_draft(db, statement)}


@router.put("/statements/{statement_id}")
def edit_statement(statement_id: str, body: StatementBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    return {"ok": True, "statement": payout_policy.update_statement(db, statement_id, body.model_dump(exclude_unset=True))}


@router.post("/statements/{statement_id}/approve")
def approve_payout_statement(statement_id: str, body: StatementBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    return {"ok": True, "statement": payout_policy.approve_statement(db, statement_id, body.note or "")}


@router.post("/statements/{statement_id}/reject")
def reject_payout_statement(statement_id: str, body: StatementBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    return {"ok": True, "statement": payout_policy.reject_statement(db, statement_id, body.note or "")}


@router.get("/statements/{statement_id}/csv")
def statement_csv(statement_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    from app import payout_policy

    statement = payout_policy._find(payout_policy.list_statements(db), statement_id)
    return PlainTextResponse(
        payout_policy.admin_csv(statement),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{statement_id}.csv"'},
    )


