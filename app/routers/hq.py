"""HQ (admin) — overview metrics and customer account desk."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FeatureRecord, User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/hq", tags=["hq"])

PRICE = {"solo": 29, "pro": 79, "premium": 399, "ent": 1250}


class CustomerActionBody(BaseModel):
    action: str = Field(..., description="free_month|refund|change_plan|cancel|impersonate")
    plan: str | None = None
    reason: str = "Customer asked"


class PartnersStateBody(BaseModel):
    run: dict | None = None
    paid: bool | None = None
    apps: dict | None = None
    rates: dict | None = None
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
            {"id": "sam", "name": "Sam Ortiz", "type": "Partner since May 2026", "rate": 30, "founding": False, "qual": 38, "paying": 41, "life": 5779.7, "flag": ""},
            {"id": "wp", "name": "WP Agency Collective", "type": "Partner since Jun 2026", "rate": 30, "founding": False, "qual": 15, "paying": 17, "life": 2104.9, "flag": ""},
            {"id": "lml", "name": "Local Marketing Lab", "type": "Partner since Jul 2026", "rate": 30, "founding": False, "qual": 8, "paying": 9, "life": 744.0, "flag": "Tax details missing"},
            {"id": "dana", "name": "Dana Kim", "type": "Partner since Aug 2026", "rate": 30, "founding": False, "qual": 4, "paying": 4, "life": 128.4, "flag": "2 sign-ups used Dana's card"},
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
    """Idempotent HQ demo payloads on the admin account."""
    admin = db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id).first()
    if admin is None:
        return
    if not _feature(db, admin.id, "hq-overview"):
        _save_feature(db, admin.id, "hq-overview", "HQ overview", _demo_overview())
    if not _feature(db, admin.id, "hq-customers"):
        _save_feature(db, admin.id, "hq-customers", "HQ customers", _demo_customers())
    if not _feature(db, admin.id, "hq-partners"):
        _save_feature(db, admin.id, "hq-partners", "HQ partners", _demo_partners())
    if not _feature(db, admin.id, "hq-problems"):
        _save_feature(db, admin.id, "hq-problems", "HQ problems", _demo_problems())
    if not _feature(db, admin.id, "hq-shareholders"):
        _save_feature(db, admin.id, "hq-shareholders", "HQ shareholders", _demo_shareholders())


@router.get("/overview")
def overview(
    range: str = Query("30", pattern="^(30|90|365)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    payload = {**_demo_overview(), **_feature(db, admin.id, "hq-overview")}
    return {"ok": True, "live": True, "range": range, **payload}


@router.get("/customers")
def list_customers(
    q: str = "",
    status: str = "all",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    data = {**_demo_customers(), **_feature(db, admin.id, "hq-customers")}
    items = list(data.get("items") or [])
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
    return {
        "ok": True,
        "live": True,
        "total": int(data.get("total") or len(items)),
        "items": items,
    }


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    data = {**_demo_customers(), **_feature(db, admin.id, "hq-customers")}
    for c in data.get("items") or []:
        if c.get("id") == customer_id:
            return {"ok": True, "live": True, "customer": c}
    raise HTTPException(404, "Customer not found")


@router.put("/customers/{customer_id}")
def act_customer(
    customer_id: str,
    body: CustomerActionBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    data = {**_demo_customers(), **_feature(db, admin.id, "hq-customers")}
    items = list(data.get("items") or [])
    idx = next((i for i, c in enumerate(items) if c.get("id") == customer_id), None)
    if idx is None:
        raise HTTPException(404, "Customer not found")
    c = dict(items[idx])
    plan_key = c.get("plan") or "solo"
    price = PRICE.get(plan_key, 29)
    reason = (body.reason or "Customer asked").strip()
    who = user.username or "Admin"
    when = datetime.utcnow().strftime("%b %d")
    log = list(c.get("log") or [])

    action = body.action
    msg = "Updated."
    if action == "impersonate":
        msg = f"Open workspace as {c.get('owner')} (session hand-off is local-only for now)."
        log.insert(0, {"when": when, "text": f"Opened as customer · reason: {reason}", "who": who})
    elif action == "free_month":
        c["freeMonths"] = int(c.get("freeMonths") or 0) + 1
        c["next"] = "Skipped · then Dec"
        log.insert(0, {"when": when, "text": f"Gave a free month · reason: {reason}", "who": who})
        msg = f"Free month added. {c.get('owner')} has been emailed."
    elif action == "refund":
        c["refunded"] = True
        c["ltv"] = max(0, float(c.get("ltv") or 0) - price)
        log.insert(0, {"when": when, "text": f"Refunded ${price:.2f} · reason: {reason}", "who": who})
        msg = "Refund sent. The partner commission on it has been reversed."
    elif action == "change_plan":
        new_plan = (body.plan or plan_key).lower()
        if new_plan not in ("solo", "pro", "premium"):
            raise HTTPException(400, "Unknown plan")
        c["plan"] = new_plan
        log.insert(0, {"when": when, "text": f"Moved to {new_plan} · reason: {reason}", "who": who})
        msg = f"Plan updated to {new_plan}."
    elif action == "cancel":
        c["status"] = "cancelled"
        c["next"] = "None"
        log.insert(0, {"when": when, "text": f"Cancelled subscription · reason: {reason}", "who": who})
        msg = "Subscription cancelled."
    else:
        raise HTTPException(400, "Unknown action")

    c["log"] = log[:40]
    items[idx] = c
    data["items"] = items
    _save_feature(db, admin.id, "hq-customers", "HQ customers", data)
    return {"ok": True, "message": msg, "customer": c}


def _partners_payload(db: Session, admin: User) -> dict:
    ensure_hq_demo(db)
    base = _demo_partners()
    stored = _feature(db, admin.id, "hq-partners")
    merged = {**base, **stored}
    state = {**(base.get("state") or {}), **(stored.get("state") or {})}
    merged["state"] = state
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
    merged["partners"] = [*_real_partners(db), *[p for p in (merged.get("partners") or []) if not str(p.get("id", "")).startswith("u")]]
    return merged


def _real_partners(db: Session) -> list[dict]:
    """Partner accounts that exist in the database (approved applications and the seeded partner)."""
    from app.routers.partners import PRICE, _default_profile

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
            "type": ("Founding partner" if founding else "Partner") + f" since {profile.get('since') or ''}".rstrip(),
            "rate": int(round(float(profile.get("rate") or 0.3) * 100)),
            "founding": founding,
            "qual": int(profile.get("qualified") or 0),
            "paying": paying,
            "life": round(life, 2),
            "flag": "",
            "real": True,
        })
    return out


class DecisionBody(BaseModel):
    rate: float = 30
    founding: bool = False


@router.post("/partners/applications/{app_id}/approve")
def approve_partner(app_id: str, body: DecisionBody | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Create the partner account and send the set-password link. Returns the link when email is not set up."""
    _require_admin(user)
    from app.routers.partners import approve_application

    body = body or DecisionBody()
    rate = max(1.0, min(60.0, float(body.rate or 30))) / 100
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
    data = _partners_payload(db, admin)
    state = dict(data.get("state") or {})
    if body.run is not None:
        state["run"] = {**state.get("run", {}), **body.run}
    if body.paid is not None:
        state["paid"] = body.paid
    if body.apps is not None:
        state["apps"] = {**state.get("apps", {}), **body.apps}
    if body.rates is not None:
        state["rates"] = {**state.get("rates", {}), **body.rates}
    if body.cleared is not None:
        state["cleared"] = {**state.get("cleared", {}), **body.cleared}
    if body.grants is not None:
        state["grants"] = {**state.get("grants", {}), **body.grants}
    data["state"] = state
    _save_feature(db, admin.id, "hq-partners", "HQ partners", data)
    return {"ok": True, "live": True, **data}


@router.get("/problems")
def get_problems(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    base = _demo_problems()
    stored = _feature(db, admin.id, "hq-problems")
    data = {**base, **stored}
    data["done"] = {**(base.get("done") or {}), **(stored.get("done") or {})}
    data["emailed"] = {**(base.get("emailed") or {}), **(stored.get("emailed") or {})}
    open_n = len([i for i in (data.get("items") or []) if not data["done"].get(i.get("id"))])
    return {"ok": True, "live": True, "openCount": open_n, **data}


@router.put("/problems/{problem_id}")
def act_problem(
    problem_id: str,
    body: ProblemActionBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    base = _demo_problems()
    stored = _feature(db, admin.id, "hq-problems")
    data = {**base, **stored}
    data["done"] = {**(base.get("done") or {}), **(stored.get("done") or {})}
    data["emailed"] = {**(base.get("emailed") or {}), **(stored.get("emailed") or {})}
    ids = {i.get("id") for i in (data.get("items") or [])}
    if problem_id not in ids:
        raise HTTPException(404, "Problem not found")
    if body.action == "resolve":
        data["done"][problem_id] = True
        msg = "Resolved."
    elif body.action == "email":
        data["emailed"][problem_id] = True
        msg = "Email sent."
    else:
        raise HTTPException(400, "Unknown action")
    _save_feature(db, admin.id, "hq-problems", "HQ problems", data)
    open_n = len([i for i in (data.get("items") or []) if not data["done"].get(i.get("id"))])
    return {"ok": True, "message": msg, "openCount": open_n, "done": data["done"], "emailed": data["emailed"]}


@router.get("/shareholders")
def get_shareholders(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    base = _demo_shareholders()
    stored = _feature(db, admin.id, "hq-shareholders")
    data = {**base, **stored}
    data["state"] = {**(base.get("state") or {}), **(stored.get("state") or {})}
    data["scenarios"] = {**(base.get("scenarios") or {}), **(stored.get("scenarios") or {})}
    return {"ok": True, "live": True, **data}


@router.put("/shareholders")
def put_shareholders(
    body: ShareholdersStateBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _require_admin(user)
    admin = _admin_owner(db)
    ensure_hq_demo(db)
    base = _demo_shareholders()
    stored = _feature(db, admin.id, "hq-shareholders")
    data = {**base, **stored}
    state = {**(base.get("state") or {}), **(stored.get("state") or {})}
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
    data["state"] = state
    _save_feature(db, admin.id, "hq-shareholders", "HQ shareholders", data)
    return {"ok": True, "live": True, **data}
