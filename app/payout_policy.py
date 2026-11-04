"""Monthly payout statement. Every rate, window, hold, minimum, and split is a setting."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import FeatureRecord, User

DEFAULTS = {
    "affiliateSolo": 0.25,
    "affiliatePro": 0.25,
    "affiliateAgency": 0.25,
    "affiliateManaged": 0.20,
    "partnerSolo": 0.35,
    "partnerPro": 0.35,
    "partnerAgency": 0.35,
    "partnerManaged": 0.30,
    "affiliateMonths": 12,
    "partnerMonths": 0,
    "holdDays": 30,
    "minimumPayout": 50,
    "reserveRate": 0.20,
    "cashCoverMonths": 3,
    "profitableMonthsRequired": 6,
    "reserveBalance": 0,
    "carryForward": 0,
    "owners": [
        {"id": "owner", "name": "Owner", "pct": 0.80, "userId": None},
        {"id": "partner-a", "name": "Partner A", "pct": 0.10, "userId": None},
        {"id": "partner-b", "name": "Partner B", "pct": 0.05, "userId": None},
        {"id": "partner-c", "name": "Partner C", "pct": 0.05, "userId": None},
    ],
}

COST_KEYS = ("dataforseo", "openai", "aws", "maintenance", "tools", "salary")


def _feature(db: Session, user_id: int, kind: str) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict((row.payload if row else {}) or {})


def _save(db: Session, user_id: int, kind: str, title: str, payload: dict) -> dict:
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


def _admin(db: Session) -> User:
    admin = db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id.asc()).first()
    if admin is None:
        raise HTTPException(500, "No admin account to store payout settings.")
    return admin


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def load_settings(db: Session) -> dict:
    stored = _feature(db, _admin(db).id, "payout-settings")
    settings = {**DEFAULTS, **{k: v for k, v in stored.items() if k != "owners"}}
    if isinstance(stored.get("owners"), list) and stored["owners"]:
        settings["owners"] = stored["owners"]
    return settings


def save_settings(db: Session, patch: dict) -> dict:
    current = load_settings(db)
    for key in (
        "affiliateSolo", "affiliatePro", "affiliateAgency", "affiliateManaged",
        "partnerSolo", "partnerPro", "partnerAgency", "partnerManaged",
        "reserveRate",
    ):
        if key in patch and patch[key] is not None:
            rate = _num(patch[key])
            if rate > 1:
                rate = rate / 100.0
            if not 0 <= rate <= 1:
                raise HTTPException(400, f"{key} must be between 0 and 100%.")
            current[key] = round(rate, 4)
    for key in ("affiliateMonths", "partnerMonths", "holdDays", "cashCoverMonths", "profitableMonthsRequired"):
        if key in patch and patch[key] is not None:
            current[key] = max(0, int(_num(patch[key])))
    if patch.get("minimumPayout") is not None:
        current["minimumPayout"] = max(0, round(_num(patch["minimumPayout"]), 2))
    if isinstance(patch.get("owners"), list):
        owners = []
        for row in patch["owners"]:
            name = " ".join(str(row.get("name") or "").split())[:80]
            if not name:
                continue
            pct = _num(row.get("pct"))
            if pct > 1:
                pct = pct / 100.0
            owners.append({
                "id": str(row.get("id") or name.lower().replace(" ", "-"))[:40],
                "name": name,
                "pct": round(pct, 4),
                "userId": int(row["userId"]) if str(row.get("userId") or "").isdigit() else None,
            })
        total = round(sum(item["pct"] for item in owners), 4)
        if abs(total - 1) > 0.001:
            raise HTTPException(400, f"Ownership must add up to 100%. It is {round(total * 100, 2)}%.")
        current["owners"] = owners
    _save(db, _admin(db).id, "payout-settings", "Payout settings", current)
    return current


def plan_key(plan: str) -> str:
    raw = str(plan or "").strip().lower()
    if "managed" in raw:
        return "managed"
    if raw in {"pro", "growth"}:
        return "pro"
    if raw in {"agency", "premium", "scale"}:
        return "agency"
    return "solo"


def is_business_partner(profile: dict) -> bool:
    return str((profile or {}).get("tier") or "") == "founding"


def rate_for(settings: dict, profile: dict, plan: str) -> float:
    key = plan_key(plan)
    if is_business_partner(profile):
        return _num(settings.get(f"partner{key.title()}" if key != "managed" else "partnerManaged"))
    if key == "managed":
        return _num(settings.get("affiliateManaged"))
    return _num(settings.get(f"affiliate{key.title()}"))


def still_earning(settings: dict, profile: dict, paid_at: datetime, joined: datetime) -> bool:
    months = int(settings.get("partnerMonths") if is_business_partner(profile) else settings.get("affiliateMonths") or 0)
    if months <= 0:
        return True
    return (paid_at - joined).days <= int(months * 365 / 12)


def hold_days(settings: dict) -> int:
    return max(0, int(settings.get("holdDays") or 0))


def minimum_payout(settings: dict) -> float:
    return max(0, _num(settings.get("minimumPayout"), 50))


def record_revenue(db: Session, *, amount: float, plan: str, user_id: int, kind: str = "payment", at: datetime | None = None) -> None:
    admin = _admin(db)
    book = _feature(db, admin.id, "billing-revenue")
    rows = list(book.get("rows") or [])
    rows.insert(0, {
        "at": (at or datetime.utcnow()).isoformat(timespec="seconds"),
        "amount": round(float(amount), 2),
        "plan": plan_key(plan),
        "userId": user_id,
        "kind": kind,
    })
    _save(db, admin.id, "billing-revenue", "Billing revenue", {"rows": rows[:2000]})


def _month_of(stamp: str) -> str:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "")) .strftime("%Y-%m")
    except ValueError:
        return ""


def _store(db: Session) -> dict:
    return _feature(db, _admin(db).id, "payout-statements")


def _write_store(db: Session, payload: dict) -> dict:
    return _save(db, _admin(db).id, "payout-statements", "Payout statements", payload)


def list_statements(db: Session) -> list[dict]:
    return list(_store(db).get("rows") or [])


def _find(rows: list[dict], statement_id: str) -> dict:
    for row in rows:
        if row.get("id") == statement_id:
            return row
    raise HTTPException(404, "Statement not found.")


def _commissions_for_month(db: Session, month: str) -> list[dict]:
    lines = []
    settings = load_settings(db)
    partners = db.query(User).filter(User.role == "ROLE_PARTNER").all()
    for partner in partners:
        ledger = _feature(db, partner.id, "partner-ledger")
        profile = _feature(db, partner.id, "partner-profile")
        for row in ledger.get("earnings") or []:
            if not isinstance(row, list) or len(row) < 7:
                continue
            due = str(row[5] or "")
            try:
                paid = datetime.fromisoformat(due.replace("Z", "")) - timedelta(days=hold_days(settings))
            except ValueError:
                continue
            if paid.strftime("%Y-%m") != month:
                continue
            amount = _num(row[6])
            if amount == 0:
                continue
            lines.append({
                "userId": partner.id,
                "name": profile.get("displayName") or partner.username,
                "tier": "partner" if is_business_partner(profile) else "affiliate",
                "plan": row[2],
                "status": row[3],
                "amount": round(amount, 2),
                "payable": row[3] in {"cleared", "paid"},
            })
    return lines


def _revenue_for_month(db: Session, month: str) -> dict:
    rows = _feature(db, _admin(db).id, "billing-revenue").get("rows") or []
    gross = refunds = 0.0
    for row in rows:
        if _month_of(row.get("at") or "") != month:
            continue
        amount = _num(row.get("amount"))
        if row.get("kind") == "refund":
            refunds += amount
        else:
            gross += amount
    return {"revenueGross": round(gross, 2), "refunds": round(refunds, 2)}


def _costs(raw: dict | None) -> dict:
    src = raw or {}
    return {key: round(max(0, _num(src.get(key))), 2) for key in COST_KEYS}


def _profitable_streak(rows: list[dict], month: str, required: int) -> int:
    approved = [row for row in rows if row.get("status") == "approved" and str(row.get("month")) < month]
    approved.sort(key=lambda row: str(row.get("month")))
    streak = 0
    for row in reversed(approved):
        if _num(row.get("profit")) > 0:
            streak += 1
        else:
            break
    return streak


def build_statement(db: Session, month: str, *, costs: dict | None = None, bank_balance: float | None = None, note: str = "", revenue: dict | None = None) -> dict:
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(400, "Month must look like 2026-11.")
    settings = load_settings(db)
    money = _revenue_for_month(db, month)
    if revenue:
        for key in ("revenueGross", "stripeFees", "refunds", "salesTax"):
            if revenue.get(key) is not None:
                money[key] = round(_num(revenue.get(key)), 2)
    money.setdefault("stripeFees", 0)
    money.setdefault("salesTax", 0)
    revenue_net = round(money["revenueGross"] - money.get("stripeFees", 0) - money.get("refunds", 0) - money.get("salesTax", 0), 2)
    lines = _commissions_for_month(db, month)
    commission_total = round(sum(line["amount"] for line in lines if line["amount"] > 0), 2)
    cost = _costs(costs)
    cost_total = round(sum(cost.values()), 2)
    profit = round(revenue_net - commission_total - cost_total, 2)
    reserve = round(max(0, profit) * _num(settings.get("reserveRate"), 0.2), 2)
    distributable = round(max(0, profit - reserve) + _num(settings.get("carryForward")), 2)
    rows = list_statements(db)
    streak = _profitable_streak(rows, month, int(settings.get("profitableMonthsRequired") or 6))
    if profit > 0:
        streak += 1
    monthly_costs = cost_total
    cover_needed = round(monthly_costs * int(settings.get("cashCoverMonths") or 3), 2)
    cash_on_hand = round(_num(settings.get("reserveBalance")) + _num(bank_balance), 2)
    cash_ok = monthly_costs <= 0 or cash_on_hand >= cover_needed
    profit_ok = streak >= int(settings.get("profitableMonthsRequired") or 6)
    reasons = []
    if not cash_ok:
        reasons.append(
            f"Reserve plus bank balance (${cash_on_hand:.2f}) is under {settings.get('cashCoverMonths')} months of costs (${cover_needed:.2f})."
        )
    if not profit_ok:
        reasons.append(
            f"{streak} profitable month(s) in a row. {settings.get('profitableMonthsRequired')} are required."
        )
    blocked = bool(reasons)
    owners = []
    for owner in settings.get("owners") or []:
        share = 0 if blocked else round(distributable * _num(owner.get("pct")), 2)
        owners.append({**owner, "amount": share})
    statement = {
        "id": f"stmt-{month}",
        "month": month,
        "status": "draft",
        "revenueGross": money["revenueGross"],
        "stripeFees": money.get("stripeFees", 0),
        "refunds": money.get("refunds", 0),
        "salesTax": money.get("salesTax", 0),
        "revenueNet": revenue_net,
        "commissions": lines,
        "commissionTotal": commission_total,
        "costs": cost,
        "costTotal": cost_total,
        "profit": profit,
        "reserveRate": settings.get("reserveRate"),
        "reserveAmount": 0 if blocked else reserve,
        "reserveHeld": reserve,
        "distributable": 0 if blocked else distributable,
        "carried": distributable if blocked else 0,
        "blocked": blocked,
        "blockedReason": " ".join(reasons),
        "bankBalance": round(_num(bank_balance), 2),
        "cashOnHand": cash_on_hand,
        "coverNeeded": cover_needed,
        "profitableStreak": streak,
        "owners": owners,
        "note": note or "",
        "minimumPayout": minimum_payout(settings),
        "updatedAt": datetime.utcnow().isoformat() + "Z",
        "history": [{"at": datetime.utcnow().isoformat() + "Z", "action": "draft", "note": note or ""}],
    }
    return statement


def upsert_draft(db: Session, statement: dict) -> dict:
    rows = list_statements(db)
    rows = [row for row in rows if row.get("id") != statement["id"]]
    rows.insert(0, statement)
    _write_store(db, {"rows": rows[:120]})
    return statement


def update_statement(db: Session, statement_id: str, patch: dict) -> dict:
    rows = list_statements(db)
    current = _find(rows, statement_id)
    if current.get("status") == "approved":
        raise HTTPException(400, "An approved statement cannot be edited. Reject it first if you need a new draft.")
    rebuilt = build_statement(
        db,
        current["month"],
        costs=patch.get("costs") if patch.get("costs") is not None else current.get("costs"),
        bank_balance=patch.get("bankBalance") if patch.get("bankBalance") is not None else current.get("bankBalance"),
        note=patch.get("note") if patch.get("note") is not None else current.get("note") or "",
        revenue={
            "revenueGross": patch.get("revenueGross", current.get("revenueGross")),
            "stripeFees": patch.get("stripeFees", current.get("stripeFees")),
            "refunds": patch.get("refunds", current.get("refunds")),
            "salesTax": patch.get("salesTax", current.get("salesTax")),
        },
    )
    rebuilt["history"] = list(current.get("history") or []) + list(rebuilt.get("history") or [])
    rebuilt["history"].append({"at": datetime.utcnow().isoformat() + "Z", "action": "edited", "note": patch.get("note") or ""})
    return upsert_draft(db, rebuilt)


def approve_statement(db: Session, statement_id: str, note: str = "") -> dict:
    rows = list_statements(db)
    current = _find(rows, statement_id)
    if current.get("status") == "approved":
        return current
    settings = load_settings(db)
    if current.get("blocked"):
        settings["carryForward"] = round(_num(current.get("carried")), 2)
    else:
        settings["carryForward"] = 0
        settings["reserveBalance"] = round(_num(settings.get("reserveBalance")) + _num(current.get("reserveAmount")), 2)
    _save(db, _admin(db).id, "payout-settings", "Payout settings", settings)
    current["status"] = "approved"
    current["approvedAt"] = datetime.utcnow().isoformat() + "Z"
    current["history"] = list(current.get("history") or []) + [{
        "at": current["approvedAt"],
        "action": "approved",
        "note": note or "Approved. Pay each person by PayPal, Wise, or bank. Nothing is sent automatically.",
    }]
    return upsert_draft(db, current)


def reject_statement(db: Session, statement_id: str, note: str = "") -> dict:
    rows = list_statements(db)
    current = _find(rows, statement_id)
    current["status"] = "rejected"
    current["history"] = list(current.get("history") or []) + [{
        "at": datetime.utcnow().isoformat() + "Z",
        "action": "rejected",
        "note": note or "Rejected. Recalculate before approving.",
    }]
    return upsert_draft(db, current)


def _person_rows(statement: dict, user_id: int | None, owner_id: str | None) -> list[dict]:
    """One person's lines. Never includes other owners."""
    out = []
    for line in statement.get("commissions") or []:
        if user_id is not None and int(line.get("userId") or 0) == int(user_id):
            out.append({"month": statement.get("month"), "kind": "commission", "amount": line.get("amount"), "plan": line.get("plan"), "status": line.get("status")})
    for owner in statement.get("owners") or []:
        if owner_id and owner.get("id") == owner_id:
            out.append({"month": statement.get("month"), "kind": "distribution", "amount": owner.get("amount"), "pct": owner.get("pct"), "status": statement.get("status")})
        elif user_id is not None and owner.get("userId") and int(owner["userId"]) == int(user_id):
            out.append({"month": statement.get("month"), "kind": "distribution", "amount": owner.get("amount"), "pct": owner.get("pct"), "status": statement.get("status")})
    return out


def statements_for_user(db: Session, user_id: int) -> list[dict]:
    rows = []
    for statement in list_statements(db):
        if statement.get("status") != "approved":
            continue
        rows.extend(_person_rows(statement, user_id, None))
    return rows


def csv_for_lines(lines: list[dict], title: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([title])
    writer.writerow(["month", "kind", "plan", "status", "ownership", "amount"])
    for line in lines:
        writer.writerow([
            line.get("month") or "",
            line.get("kind") or "",
            line.get("plan") or "",
            line.get("status") or "",
            line.get("pct") or "",
            line.get("amount") or 0,
        ])
    return buf.getvalue()


def admin_csv(statement: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Searchify payout statement", statement.get("month"), statement.get("status")])
    writer.writerow(["step", "item", "amount"])
    writer.writerow(["1", "Revenue collected", statement.get("revenueGross")])
    writer.writerow(["1", "Stripe fees", statement.get("stripeFees")])
    writer.writerow(["1", "Refunds", statement.get("refunds")])
    writer.writerow(["1", "Sales tax", statement.get("salesTax")])
    writer.writerow(["1", "Net revenue", statement.get("revenueNet")])
    writer.writerow(["2", "Commissions", statement.get("commissionTotal")])
    for key, amount in (statement.get("costs") or {}).items():
        writer.writerow(["3", key, amount])
    writer.writerow(["3", "Profit", statement.get("profit")])
    writer.writerow(["4", "Reserve", statement.get("reserveAmount")])
    writer.writerow(["5", "Distributable", statement.get("distributable")])
    writer.writerow(["5", "Carried because gates failed", statement.get("carried")])
    writer.writerow(["gate", statement.get("blockedReason") or "Gates passed", ""])
    for owner in statement.get("owners") or []:
        writer.writerow(["5", owner.get("name"), owner.get("amount")])
    return buf.getvalue()
