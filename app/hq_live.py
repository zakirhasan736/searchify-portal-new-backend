"""HQ screens computed from accounts, connections, changes, and partner ledgers."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import CmsConnection, CrawlSnapshot, FeatureRecord, GoogleConnection, Job, SiteChange, User
from app.site_profiles import host_of

PLAN_KEY = {
    "starter": "solo",
    "solo": "solo",
    "growth": "pro",
    "pro": "pro",
    "agency": "premium",
    "premium": "premium",
    "scale": "premium",
}
PRICE = {"solo": 29, "pro": 79, "premium": 399, "ent": 0}
SITE_LIMIT = {"solo": 1, "pro": 5, "premium": 25}
PLAN_LABEL = {"solo": "Solo", "pro": "Pro", "premium": "Premium"}
SHIPPED = {"applied", "monitoring", "closed"}
UNDONE = {"failed", "rolled_back", "reverted"}


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
        db.add(FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload))
    else:
        row.payload = payload
        row.title = title
    db.commit()
    return payload


def clients(db: Session) -> list[User]:
    return db.query(User).filter(User.role == "ROLE_CLIENT").order_by(User.created_at.desc()).all()


def plan_key(raw: str | None) -> str:
    return PLAN_KEY.get(str(raw or "").lower(), "solo")


def _when(dt: datetime | None, fmt: str = "%b %Y") -> str:
    if not dt:
        return ""
    return dt.strftime(fmt).replace(" 0", " ")


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "recently"
    delta = datetime.utcnow() - dt
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "just now"
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = delta.days
    return f"{days} day{'s' if days != 1 else ''} ago"


def _status(checkout: dict) -> str:
    raw = str(checkout.get("status") or "").lower()
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
    if raw in {"failed", "past_due", "declined"}:
        return "failed"
    if raw in {"active", "paying"}:
        return "paying"
    if raw == "trial" or checkout:
        return "trial"
    return "trial"


def _next_bill(user: User, checkout: dict, status: str, free_months: int) -> str:
    if free_months:
        return f"{free_months} free month{'s' if free_months != 1 else ''} credited"
    if status == "cancelled":
        return "None"
    if status == "failed":
        return "Payment needs a retry"
    start = user.created_at or datetime.utcnow()
    try:
        stamp = str(checkout.get("at") or "").replace("Z", "")
        if stamp:
            start = datetime.fromisoformat(stamp)
    except ValueError:
        pass
    if status == "trial":
        return f"Trial ends {_when(start + timedelta(days=14), '%b %d')}"
    day = min(start.day, 28)
    today = datetime.utcnow()
    month = today.month + (0 if today.day < day else 1)
    year = today.year + (1 if month > 12 else 0)
    month = month if month <= 12 else month - 12
    return f"Next bill {_when(datetime(year, month, day), '%b %d')}"


def _payments(user: User, checkout: dict, status: str, price: int, overlay: dict) -> tuple[list[dict], float]:
    stored = overlay.get("payments")
    if isinstance(stored, list) and stored:
        rows = []
        for row in stored:
            if not isinstance(row, dict):
                continue
            rows.append({
                "date": row.get("date") or "",
                "amount": float(row.get("amount") or 0),
                "status": row.get("status") or "Paid",
            })
        ltv = sum(r["amount"] for r in rows if r["status"] == "Paid")
        ltv -= sum(r["amount"] for r in rows if r["status"] == "Refunded")
        return rows, max(0, round(ltv, 2))
    if status != "paying" or price <= 0:
        return [], 0.0
    start = user.created_at or datetime.utcnow()
    months = max(1, (datetime.utcnow().year - start.year) * 12 + datetime.utcnow().month - start.month + 1)
    months = min(months, 24)
    rows = []
    cursor = start
    for _ in range(months):
        rows.append({"date": _when(cursor, "%b %d, %Y"), "amount": price, "status": "Paid"})
        month = cursor.month + 1
        year = cursor.year + (1 if month > 12 else 0)
        cursor = datetime(year, month if month <= 12 else month - 12, min(cursor.day, 28))
    if overlay.get("refunded") and rows:
        rows[-1]["status"] = "Refunded"
    ltv = sum(r["amount"] for r in rows if r["status"] == "Paid")
    return rows, float(ltv)


def _sites(journey: dict, cms_rows: list[CmsConnection], google: GoogleConnection | None) -> list[list]:
    gsc_ok = bool(google and google.status == "connected" and google.gsc_site_url)
    ga_ok = bool(google and google.ga4_property_id and google.status == "connected")
    out = []
    seen = set()
    state = journey.get("state") if isinstance(journey.get("state"), dict) else {}
    for site in state.get("sites") or []:
        if not isinstance(site, dict) or site.get("status") != "ready":
            continue
        host = host_of((site.get("answers") or {}).get("site") or "")
        if not host or host in seen:
            continue
        seen.add(host)
        wp = any(c.status == "connected" and host_of(c.site_url) == host for c in cms_rows)
        out.append([host, 1 if gsc_ok else 0, 1 if wp else 0, 1 if ga_ok else 0])
    for conn in cms_rows:
        host = host_of(conn.site_url)
        if not host or host in seen:
            continue
        seen.add(host)
        out.append([host, 1 if gsc_ok else 0, 1 if conn.status == "connected" else 0, 1 if ga_ok else 0])
    if not out and google and google.gsc_site_url:
        out.append([host_of(google.gsc_site_url), 1 if gsc_ok else 0, 0, 1 if ga_ok else 0])
    return out


def _moves(db: Session, user_id: int) -> tuple[str, str, int, int]:
    rows = db.query(SiteChange.status).filter(SiteChange.customer_id == user_id).all()
    total = len(rows)
    shipped = sum(1 for (st,) in rows if st in SHIPPED)
    undone = sum(1 for (st,) in rows if st in UNDONE)
    undo = "0 undone" if not total or not undone else f"{round(100 * undone / total, 1)}% undone"
    return str(shipped), undo, shipped, undone


def customer_row(db: Session, user: User) -> dict:
    checkout = _feature(db, user.id, "billing-checkout")
    overlay = _feature(db, user.id, "hq-account")
    journey = _feature(db, user.id, "journey")
    referred = _feature(db, user.id, "referred-by")
    cms_rows = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).all()
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).one_or_none()
    key = plan_key(checkout.get("canonical") or user.plan)
    price = PRICE.get(key, 29)
    status = str(overlay.get("status") or _status(checkout))
    sites = _sites(journey, cms_rows, google)
    moves, undo, _, _ = _moves(db, user.id)
    payments, ltv = _payments(user, checkout, status, price, overlay)
    partner = None
    if referred.get("partnerId"):
        partner = db.get(User, int(referred["partnerId"]))
    ref = "Direct"
    if partner is not None:
        profile = _feature(db, partner.id, "partner-profile")
        ref = f"{profile.get('displayName') or partner.username} (partner)"
    free_months = int(overlay.get("freeMonths") or 0)
    log = list(overlay.get("log") or [])
    if not any("Signed up" in str(item.get("text") or "") for item in log):
        log.append({
            "when": _when(user.created_at, "%b %d"),
            "text": f"Signed up on {PLAN_LABEL.get(key, 'Solo')}" + ("" if ref == "Direct" else f" through {ref}"),
            "who": "System",
        })
    return {
        "id": str(user.id),
        "name": user.username or user.email,
        "owner": user.username or user.email,
        "email": user.email,
        "domain": sites[0][0] if sites else "",
        "plan": key,
        "status": status,
        "since": _when(user.created_at),
        "ref": ref,
        "card": overlay.get("card") or "No card on file",
        "next": _next_bill(user, checkout, status, free_months),
        "moves": moves,
        "undo": undo,
        "ltv": ltv,
        "freeMonths": free_months,
        "refunded": bool(overlay.get("refunded")),
        "payments": payments,
        "sites": sites,
        "log": log[:40],
        "createdAt": (user.created_at or datetime.utcnow()).isoformat(),
    }


def customer_list(db: Session) -> list[dict]:
    return [customer_row(db, user) for user in clients(db)]


def _month_starts(count: int = 12) -> list[datetime]:
    now = datetime.utcnow()
    year, month = now.year, now.month
    starts = []
    for offset in range(count - 1, -1, -1):
        m = month - offset
        y = year
        while m <= 0:
            m += 12
            y -= 1
        starts.append(datetime(y, m, 1))
    return starts


def _created(row: dict) -> datetime:
    try:
        return datetime.fromisoformat(str(row.get("createdAt") or "").replace("Z", ""))
    except ValueError:
        return datetime.utcnow()


def overview(db: Session, rows: list[dict], problems_count: int) -> dict:
    starts = _month_starts(12)
    months = [start.strftime("%b") for start in starts]
    plan_counts = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else datetime.utcnow() + timedelta(days=1)
        bucket = [0, 0, 0, 0]
        for row in rows:
            created = _created(row)
            if created >= end or row.get("status") == "cancelled":
                continue
            key = row.get("plan") or "solo"
            slot = {"solo": 0, "pro": 1, "premium": 2}.get(key, 0)
            if created < end:
                bucket[slot] += 1
        plan_counts.append(bucket)

    def window(days: int) -> list[int]:
        cutoff = datetime.utcnow() - timedelta(days=days)
        picked = [row for row in rows if _created(row) >= cutoff]
        signups = len(picked)
        scans = db.query(CrawlSnapshot).filter(CrawlSnapshot.created_at >= cutoff).count()
        activated = sum(1 for row in picked if any((site[1] and site[2]) for site in (row.get("sites") or []) if len(site) > 2))
        paying = sum(1 for row in picked if row.get("status") == "paying")
        visitors = scans
        return [visitors, scans, signups, activated, paying]

    shipped = db.query(SiteChange).filter(SiteChange.status.in_(tuple(SHIPPED))).count()
    total_changes = db.query(SiteChange).count()
    undone = db.query(SiteChange).filter(SiteChange.status.in_(tuple(UNDONE))).count()
    undo_rate = f"{round(100 * undone / total_changes, 1)}%" if total_changes else "0%"
    measured = db.query(SiteChange).filter(SiteChange.status == "closed").count()
    win_rate = f"{round(100 * measured / shipped, 1)}%" if shipped else "—"
    first_ships = []
    for user in clients(db):
        first = (
            db.query(SiteChange.applied_at)
            .filter(SiteChange.customer_id == user.id, SiteChange.applied_at.isnot(None))
            .order_by(SiteChange.applied_at.asc())
            .first()
        )
        if first and first[0] and user.created_at:
            first_ships.append((first[0] - user.created_at).total_seconds() / 86400)
    if first_ships:
        first_ships.sort()
        median = first_ships[len(first_ships) // 2]
        ship_time = f"{max(1, round(median))} d" if median >= 1 else f"{max(1, round(median * 24))} h"
    else:
        ship_time = "—"
    health_row = [shipped, undo_rate, win_rate, ship_time]
    cancelled = sum(1 for row in rows if row.get("status") == "cancelled")
    base = max(1, len(rows))
    churn = f"{round(100 * cancelled / base, 1)}%"
    affiliates = []
    for partner in partner_rows(db):
        owed = float(partner.get("cleared") or 0) + float(partner.get("hold") or 0)
        affiliates.append({
            "initials": "".join(part[:1] for part in str(partner["name"]).split()[:2]).upper() or "P",
            "name": partner["name"],
            "sub": partner.get("summary") or "",
            "owed": f"${owed:,.2f}",
            "_owed": owed,
        })
    affiliates.sort(key=lambda item: item["_owed"], reverse=True)
    for item in affiliates:
        item.pop("_owed", None)
    return {
        "months": months,
        "planCounts": plan_counts,
        "prices": PRICE,
        "funnels": {"30": window(30), "90": window(90), "365": window(365)},
        "health": {"30": health_row, "90": health_row, "365": health_row},
        "churn": churn,
        "churnNote": f"{cancelled} cancellation{'s' if cancelled != 1 else ''} · target under 3%",
        "affiliates": affiliates[:8],
        "problemsCount": problems_count,
    }


def partner_rows(db: Session) -> list[dict]:
    from app.routers.partners import _default_profile, live_ledger

    rows = []
    for user in db.query(User).filter(User.role == "ROLE_PARTNER").order_by(User.id).all():
        profile = {**_default_profile(user), **_feature(db, user.id, "partner-profile")}
        ledger = live_ledger(_feature(db, user.id, "partner-ledger"))
        payout = _feature(db, user.id, "partner-payout")
        weekly = ledger.get("weekly") or {}
        signups = sum(int(bucket.get("signups") or 0) for bucket in weekly.values())
        paying = sum(int(bucket.get("paying") or 0) for bucket in weekly.values())
        mix = ledger.get("mix") or {}
        if not paying:
            paying = sum(int(mix.get(name) or 0) for name in ("Solo", "Pro", "Premium"))
        totals = ledger.get("totals") or {}
        cleared = float(totals.get("cleared") or 0)
        hold = float(totals.get("hold") or ledger.get("hold") or 0)
        reversed_amt = float(totals.get("rev") or 0)
        paid = sum(float(item[1]) for item in (ledger.get("payouts") or []) if isinstance(item, (list, tuple)) and len(item) > 1)
        life = round(paid + cleared + hold, 2)
        founding = profile.get("tier") == "founding"
        rate = int(round(float(profile.get("rate") or (0.35 if founding else 0.3)) * 100))
        tax = bool(payout.get("certified") or payout.get("legalName") or payout.get("taxId"))
        method = str(payout.get("method") or "Not set")
        flag = "" if tax else "Tax details missing"
        since = profile.get("since") or _when(user.created_at)
        label = "Founding partner" if founding else "Partner"
        mrr = sum(int(mix.get(name) or 0) * {"Solo": 29, "Pro": 79, "Premium": 399}[name] for name in ("Solo", "Pro", "Premium"))
        rows.append({
            "id": f"u{user.id}",
            "name": profile.get("displayName") or user.username,
            "email": user.email,
            "handle": profile.get("handle") or "",
            "type": f"{label} since {since}".rstrip(),
            "runType": f"{label} · {rate}%",
            "rate": rate,
            "founding": founding,
            "qual": signups,
            "paying": paying,
            "life": life,
            "flag": flag,
            "cleared": round(cleared, 2),
            "hold": round(hold, 2),
            "rev": round(-abs(reversed_amt), 2) if reversed_amt else 0,
            "method": method,
            "tax": tax,
            "mrr": mrr,
            "summary": f"{signups} sign-ups · {paying} paying",
            "real": True,
            "createdAt": (user.created_at or datetime.utcnow()).isoformat(),
        })
    return rows


def partner_book(db: Session) -> dict:
    people = partner_rows(db)
    run = [
        {
            "id": person["id"],
            "name": person["name"],
            "type": person["runType"],
            "cleared": person["cleared"],
            "rev": person["rev"],
            "method": person["method"],
            "tax": person["tax"],
            "flag": person["flag"] if person["flag"] != "Tax details missing" else "",
        }
        for person in people
        if person["cleared"] or person["rev"] or person["hold"]
    ]
    partners = [
        {
            "id": person["id"],
            "name": person["name"],
            "email": person["email"],
            "handle": person["handle"],
            "type": person["type"],
            "rate": person["rate"],
            "founding": person["founding"],
            "qual": person["qual"],
            "paying": person["paying"],
            "life": person["life"],
            "flag": person["flag"],
            "real": True,
        }
        for person in people
    ]
    founders = []
    for person in people:
        if not person["founding"]:
            continue
        try:
            started = datetime.fromisoformat(person["createdAt"])
        except ValueError:
            started = datetime.utcnow()
        held = max(0, (datetime.utcnow().year - started.year) * 12 + datetime.utcnow().month - started.month)
        founders.append({
            "id": person["id"],
            "name": person["name"],
            "qual": person["qual"],
            "mrr": person["mrr"],
            "held": held,
        })
    return {
        "run": run,
        "partners": partners,
        "founders": founders,
        "steps": [2500, 5000, 10000, 20000, 40000],
        "state": {"run": {}, "paid": False, "apps": {}, "rates": {}, "cleared": {}, "grants": {}},
    }


def problem_items(db: Session) -> list[dict]:
    items = []
    for user in clients(db):
        checkout = _feature(db, user.id, "billing-checkout")
        if _status(checkout) == "failed":
            key = plan_key(checkout.get("canonical") or user.plan)
            items.append({
                "id": f"bill-{user.id}",
                "cat": "payment",
                "sev": "HIGH",
                "title": f"Payment failed: ${PRICE.get(key, 29):.2f} {PLAN_LABEL.get(key, 'Solo')} renewal",
                "who": user.username or user.email,
                "since": _ago(user.created_at),
                "detail": "The last checkout on this account is marked failed. Access stays until the card is updated.",
                "primaryLabel": "Email the customer",
                "doneText": "Payment failure noted and the customer was emailed.",
            })
        for conn in db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).all():
            if conn.status == "connected":
                continue
            host = host_of(conn.site_url) or conn.label or conn.provider
            items.append({
                "id": f"cms-{conn.id}",
                "cat": "connection",
                "sev": "HIGH",
                "title": f"{conn.provider.title()} connection {conn.status or 'broken'}",
                "who": f"{user.username} · {host}",
                "since": _ago(conn.updated_at or conn.created_at),
                "detail": conn.last_error or "The site connection is not active, so approved moves cannot ship.",
                "primaryLabel": "Send reconnect steps",
                "doneText": "Reconnect steps sent.",
            })
        google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).one_or_none()
        if google is not None and google.status not in {"", "connected"}:
            items.append({
                "id": f"gsc-{user.id}",
                "cat": "connection",
                "sev": "MEDIUM",
                "title": "Google connection needs attention",
                "who": f"{user.username} · {host_of(google.gsc_site_url) or google.google_email or 'Search Console'}",
                "since": _ago(google.updated_at),
                "detail": google.last_error or f"Google status is {google.status}. Rankings stop updating until it is connected again.",
                "primaryLabel": "Send reconnect steps",
                "doneText": "Reconnect steps sent.",
            })
    stale = datetime.utcnow() - timedelta(days=1)
    for job in db.query(Job).filter(Job.status.in_(("queued", "running")), Job.created_at < stale).limit(40).all():
        owner = db.get(User, job.customer_id)
        items.append({
            "id": f"job-{job.id}",
            "cat": "job",
            "sev": "MEDIUM",
            "title": f"Job stuck: {job.kind}",
            "who": (owner.username if owner else f"Customer {job.customer_id}"),
            "since": _ago(job.created_at),
            "detail": f"Status is still {job.status}.",
            "primaryLabel": "Mark as reviewed",
            "doneText": "Reviewed.",
        })
    for change in (
        db.query(SiteChange)
        .filter(SiteChange.status.in_(tuple(UNDONE)))
        .order_by(SiteChange.updated_at.desc())
        .limit(40)
        .all()
    ):
        owner = db.get(User, change.customer_id)
        items.append({
            "id": f"roll-{change.id}",
            "cat": "rollback",
            "sev": "MEDIUM",
            "title": f"Move {change.status}: {change.change_type}",
            "who": f"{owner.username if owner else 'Customer'} · {host_of(change.target_url) or change.target_url}",
            "since": _ago(change.updated_at or change.created_at),
            "detail": change.opportunity or "This change did not stay live.",
            "primaryLabel": "Mark as reviewed",
            "doneText": "Reviewed.",
        })
    return items


def _mrr_by_month(rows: list[dict]) -> list[list]:
    starts = _month_starts(6)
    series = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else datetime.utcnow() + timedelta(days=1)
        mrr = 0
        for row in rows:
            if row.get("status") != "paying":
                continue
            if _created(row) < end:
                mrr += PRICE.get(row.get("plan") or "solo", 0)
        series.append([start.strftime("%b"), mrr])
    return series


def shareholders(db: Session, rows: list[dict]) -> dict:
    paying = [row for row in rows if row.get("status") == "paying"]
    mrr = sum(PRICE.get(row.get("plan") or "solo", 0) for row in paying)
    series = _mrr_by_month(rows)
    # Profit is not the same as revenue. Expenses are not in the database, so profit stays 0
    # and a dividend stays blocked until the books record a real profit.
    profit = [[label, 0] for label, _rev in series]
    report = [
        ["Revenue this month", f"${mrr:,.0f}"],
        ["Expenses", "Not recorded"],
        ["Profit", "$0"],
        ["Cash", "Not recorded"],
        ["Paying customers", str(len(paying))],
        ["Dealings with your other businesses", "None recorded"],
        ["Change in ownership", "None recorded"],
    ]
    return {
        "holders": [],
        "scenarios": {
            "actual": {"m": profit, "cash": 0, "exp": 0, "revenue": series},
        },
        "report": report,
        "state": {
            "scenario": "actual",
            "amount": 0,
            "solvent": False,
            "prepared": False,
            "quarterly": "none",
            "howard": [],
        },
    }


def apply_customer_action(db: Session, customer_id: str, action: str, plan: str | None, reason: str, who: str) -> dict:
    try:
        user = db.get(User, int(customer_id))
    except ValueError:
        user = None
    if user is None or user.role != "ROLE_CLIENT":
        return {}
    overlay = _feature(db, user.id, "hq-account")
    checkout = _feature(db, user.id, "billing-checkout")
    log = list(overlay.get("log") or [])
    when = _when(datetime.utcnow(), "%b %d")
    reason = reason.strip() or "Customer asked"
    message = "Updated."
    if action == "impersonate":
        message = f"Recorded an open-as request for {user.username}. Sign-in as that customer still uses their own password."
        log.insert(0, {"when": when, "text": f"Opened as customer · reason: {reason}", "who": who})
    elif action == "free_month":
        overlay["freeMonths"] = int(overlay.get("freeMonths") or 0) + 1
        log.insert(0, {"when": when, "text": f"Gave a free month · reason: {reason}", "who": who})
        message = f"Free month added for {user.username}."
    elif action == "refund":
        key = plan_key(user.plan)
        price = PRICE.get(key, 29)
        payments = list(overlay.get("payments") or [])
        payments.insert(0, {"date": _when(datetime.utcnow(), "%b %d, %Y"), "amount": price, "status": "Refunded"})
        overlay["payments"] = payments[:36]
        overlay["refunded"] = True
        log.insert(0, {"when": when, "text": f"Refunded ${price:.2f} · reason: {reason}", "who": who})
        message = "Refund recorded on the account."
        from app.routers.partners import record_refund

        record_refund(db, user, price, key)
    elif action == "change_plan":
        new_plan = plan_key(plan or user.plan)
        if new_plan not in PRICE or new_plan == "ent":
            return {"error": "Unknown plan"}
        canonical = {"solo": "starter", "pro": "growth", "premium": "agency"}[new_plan]
        user.plan = canonical
        user.site_limit = SITE_LIMIT[new_plan]
        db.add(user)
        db.commit()
        checkout["canonical"] = canonical
        checkout["planId"] = new_plan
        if checkout.get("status") not in {"cancelled", "canceled", "failed"}:
            checkout["status"] = checkout.get("status") or "paying"
        _save(db, user.id, "billing-checkout", "Billing checkout", checkout)
        log.insert(0, {"when": when, "text": f"Moved to {new_plan} · reason: {reason}", "who": who})
        message = f"Plan updated to {PLAN_LABEL[new_plan]}."
    elif action == "cancel":
        overlay["status"] = "cancelled"
        checkout["status"] = "cancelled"
        _save(db, user.id, "billing-checkout", "Billing checkout", checkout or {"status": "cancelled"})
        log.insert(0, {"when": when, "text": f"Cancelled subscription · reason: {reason}", "who": who})
        message = "Subscription cancelled."
    else:
        return {"error": "Unknown action"}
    overlay["log"] = log[:40]
    _save(db, user.id, "hq-account", "HQ account", overlay)
    row = customer_row(db, user)
    return {"message": message, "customer": row}
