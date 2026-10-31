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

# Early-launch offer: first 100 real founding Pro subscribers lock $79.
# Counts are live unique users only — no design/demo seed fill.
EARLY_LAUNCH_SEED = {
    "total": 100,
    "claimed": 0,
    "claimedThisWeek": 0,
    "lastClaimed": "—",
    "proListMo": 99,
    "proLaunchMo": 79,
}
EARLY_LAUNCH = dict(EARLY_LAUNCH_SEED)
EARLY_LAUNCH_TOTAL = 100

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


def _kpi_num(payload: dict, *parts: str) -> float | None:
    """Read a numeric KPI from organic-search / feature panel rows."""
    rows = []
    for key in ("kpis",):
        val = payload.get(key)
        if isinstance(val, list):
            rows.extend(val)
    panels = payload.get("panels") if isinstance(payload.get("panels"), dict) else {}
    if isinstance(panels.get("kpis"), list):
        rows.extend(panels["kpis"])
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        label = str(row[0] or "").lower()
        if any(p in label for p in parts):
            raw = str(row[1] or "").replace(",", "").replace("%", "").strip()
            try:
                return float(raw)
            except Exception:
                continue
    return None


def _fmt_metric(n: float | int | None) -> str:
    if n is None:
        return "—"
    if isinstance(n, float) and not n.is_integer():
        return f"{n:.1f}"
    return f"{int(round(float(n))):,}"


def _pct_change(before: float | None, after: float | None, *, invert: bool = False) -> str:
    if before is None or after is None or before == 0:
        if before is None or after is None:
            return "Waiting on data"
        return "No change yet"
    delta = (before - after) if invert else (after - before)
    pct = round((delta / abs(before)) * 100)
    if invert:
        spots = round(before - after, 1)
        if spots > 0:
            return f"Up {spots:g} spots"
        if spots < 0:
            return f"Down {abs(spots):g} spots"
        return "Flat so far"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct}%"


def _normalize_since_rows(raw: list) -> list[dict]:
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        out.append(
            {
                "label": label,
                "from": str(item.get("from") if item.get("from") is not None else "—"),
                "to": str(item.get("to") if item.get("to") is not None else "—"),
                "change": str(item.get("change") or ""),
            }
        )
    return out


def _build_cancel_since(db: Session, user: User, *, joined: datetime, moves: int) -> dict:
    """Live before/after cards for Cancel.html — never uses design demo numbers."""
    stored = _feature(db, user.id, "cancel-since") or _feature(db, user.id, "results-snapshot")
    if isinstance(stored.get("since"), list) and stored.get("since"):
        rows = _normalize_since_rows(stored["since"])
        if rows:
            return {
                "since": rows,
                "sinceLive": True,
                "sinceSource": "snapshot",
                "sinceNote": str(
                    stored.get("note")
                    or f"Compared with your starting point saved on {_fmt_joined(joined)}, across your sites."
                ),
                "measuredMoves": int(stored.get("measuredMoves") or 0),
            }

    organic = _feature(db, user.id, "organic-search")
    inspection = _feature(db, user.id, "gsc-index-inspection")
    path_board = _feature(db, user.id, "path-board") or _feature(db, user.id, "page-progress")

    clicks_now = _kpi_num(organic, "click")
    pos_now = _kpi_num(organic, "avg position", "position", "rank")
    pages_found_now = None
    if inspection.get("indexed") is not None:
        try:
            pages_found_now = float(inspection.get("indexed"))
        except Exception:
            pages_found_now = None
    if pages_found_now is None:
        pages_found_now = _kpi_num(organic, "page", "indexed", "url")

    # Baseline from earliest monitoring snapshot on shipped changes.
    changes = (
        db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id)
        .order_by(SiteChange.id.asc())
        .limit(500)
        .all()
    )
    base_pos: list[float] = []
    live_pos: list[float] = []
    measured = 0
    for row in changes:
        mon = dict(row.monitoring or {})
        b = mon.get("baselinePosition")
        l = mon.get("latestPosition")
        try:
            if b is not None:
                base_pos.append(float(b))
            if l is not None:
                live_pos.append(float(l))
        except Exception:
            pass
        if row.status in {"monitoring", "applied", "shipped", "published", "closed", "done"}:
            if mon.get("latestPosition") is None or mon.get("waiting"):
                measured += 1
            elif row.status == "monitoring":
                measured += 1

    pos_from = round(sum(base_pos) / len(base_pos), 1) if base_pos else None
    pos_to = round(sum(live_pos) / len(live_pos), 1) if live_pos else (round(pos_now, 1) if pos_now is not None else None)
    if pos_from is None and pos_now is not None:
        # No baselines yet — treat current as both ends until history exists.
        pos_from = round(pos_now, 1)
        pos_to = round(pos_now, 1)

    # Clicks: prefer weekly series first/last month averages when present.
    clicks_from = None
    clicks_to = clicks_now
    weekly = organic.get("panels", {}).get("weeklyClicks") if isinstance(organic.get("panels"), dict) else None
    if not isinstance(weekly, list):
        weekly = organic.get("weeklyClicks")
    if isinstance(weekly, list) and len(weekly) >= 4:
        vals: list[float] = []
        for point in weekly:
            try:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    vals.append(float(point[1] or 0))
                elif isinstance(point, dict) and point.get("clicks") is not None:
                    vals.append(float(point.get("clicks") or 0))
            except Exception:
                continue
        if vals:
            early = vals[: max(1, len(vals) // 4)]
            late = vals[-max(1, len(vals) // 4) :]
            clicks_from = round((sum(early) / len(early)) * 4.33)
            clicks_to = round((sum(late) / len(late)) * 4.33)
    if clicks_from is None and clicks_now is not None:
        clicks_from = round(float(clicks_now))
        clicks_to = round(float(clicks_now))

    great_now = 0
    great_from = 0
    pages = path_board.get("pages") if isinstance(path_board.get("pages"), list) else path_board.get("rows")
    if isinstance(pages, list):
        for p in pages:
            if not isinstance(p, dict):
                continue
            grade = str(p.get("grade") or p.get("status") or p.get("band") or "").lower()
            if grade in {"great", "excellent", "pass", "passes", "good"}:
                great_now += 1
            if p.get("startedGreat") or p.get("baselineGreat"):
                great_from += 1
    if not pages and path_board.get("greatCount") is not None:
        try:
            great_now = int(path_board.get("greatCount") or 0)
            great_from = int(path_board.get("greatFrom") or 0)
        except Exception:
            pass

    pages_from = None
    if pages_found_now is not None:
        # Prefer stored baseline; else start ≈ current when no history.
        try:
            pages_from = float(stored.get("pagesFrom")) if stored.get("pagesFrom") is not None else float(pages_found_now)
        except Exception:
            pages_from = float(pages_found_now)

    since = [
        {
            "label": "Clicks from Google / month",
            "from": _fmt_metric(clicks_from),
            "to": _fmt_metric(clicks_to),
            "change": _pct_change(
                float(clicks_from) if clicks_from is not None else None,
                float(clicks_to) if clicks_to is not None else None,
            ),
        },
        {
            "label": "Average position",
            "from": _fmt_metric(pos_from),
            "to": _fmt_metric(pos_to),
            "change": _pct_change(
                float(pos_from) if pos_from is not None else None,
                float(pos_to) if pos_to is not None else None,
                invert=True,
            ),
        },
        {
            "label": "Pages at Great or Excellent",
            "from": _fmt_metric(great_from),
            "to": _fmt_metric(great_now),
            "change": (
                f"{great_now} page{'s' if great_now != 1 else ''} at goal"
                if great_now
                else ("No pages at goal yet" if moves else "Waiting on first scan")
            ),
        },
        {
            "label": "Pages Google has found",
            "from": _fmt_metric(pages_from),
            "to": _fmt_metric(pages_found_now),
            "change": (
                _pct_change(
                    float(pages_from) if pages_from is not None else None,
                    float(pages_found_now) if pages_found_now is not None else None,
                )
                if pages_found_now is not None
                else ("Connect Search Console to track this" if not organic else "Waiting on crawl data")
            ),
        },
    ]
    has_live = any(
        s["from"] != "—" or s["to"] != "—"
        for s in since
    )
    return {
        "since": since,
        "sinceLive": has_live,
        "sinceSource": "computed",
        "sinceNote": (
            f"Compared with your starting point saved on {_fmt_joined(joined)}, across your sites"
            + (", and with the same months last year so seasons don't skew it." if has_live else ".")
        ),
        "measuredMoves": int(measured),
    }


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
            SiteChange.status.in_(("shipped", "applied", "published", "closed", "done", "monitoring")),
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
    early = _early_launch_payload(db)
    checkout_founding = bool(checkout.get("founding")) if checkout else False
    # Existing founding subscribers keep the launch price; new Pro only while spots remain.
    founding = checkout_founding or (
        catalog_id == "pro" and bool(plan.get("founding")) and int(early.get("left") or 0) > 0
    )
    price = int(plan.get("mo") or 0)
    if founding and catalog_id == "pro":
        price = int(EARLY_LAUNCH_SEED["proLaunchMo"])
    elif catalog_id == "pro" and not founding and plan.get("listMo"):
        price = int(plan["listMo"])
    list_mo = int(plan.get("listMo") or 0) or (int(EARLY_LAUNCH_SEED["proListMo"]) if catalog_id == "pro" else None)
    per = "year" if billing == "annual" else "month"
    amount = price * 10 if billing == "annual" else price
    solo_mo = int(BY_ID["solo"]["mo"])
    since_pack = _build_cancel_since(db, user, joined=joined, moves=int(moves))
    joined_short = _fmt_day(joined)
    plan_line = f"{plan['name']} · ${amount} / {per}"
    move_bit = (
        f" · {int(moves)} moves shipped since {joined_short}"
        if moves
        else f" · joined {joined_short}"
    )
    plan_sub = f"{str(business).strip() or 'Your workspace'} · paid through {_fmt_day(paid)}{move_bit}"
    return {
        "ok": True,
        "live": True,
        "planId": plan["id"],
        "canonical": stored,
        "name": plan["name"],
        "billing": billing,
        "priceMo": price,
        "listMo": list_mo,
        "soloMo": solo_mo,
        "amount": amount,
        "per": per,
        "planLine": plan_line,
        "planSub": plan_sub,
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
        "joinedShort": joined_short,
        "paidThrough": paid.isoformat() + "Z",
        "paidThroughLabel": _fmt_day(paid),
        "movesShipped": int(moves),
        "measuredMoves": int(since_pack.get("measuredMoves") or 0),
        "business": str(business).strip() or "Your workspace",
        "since": since_pack.get("since") or [],
        "sinceLive": bool(since_pack.get("sinceLive")),
        "sinceSource": since_pack.get("sinceSource") or "computed",
        "sinceNote": since_pack.get("sinceNote") or "",
        "earlyLaunchTotal": int(early.get("total") or EARLY_LAUNCH_SEED["total"]),
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


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0))
        return dt
    except Exception:
        return None


def _ago_label(dt: datetime) -> str:
    secs = max(0, int((datetime.utcnow() - dt).total_seconds()))
    if secs < 45:
        return "just now"
    if secs < 3600:
        mins = max(1, secs // 60)
        return f"{mins} min"
    if secs < 86400:
        hrs = max(1, secs // 3600)
        return f"{hrs} hr" if hrs == 1 else f"{hrs} hrs"
    days = max(1, secs // 86400)
    if days < 14:
        return "1 day" if days == 1 else f"{days} days"
    weeks = days // 7
    return "1 wk" if weeks == 1 else f"{weeks} wks"


def _early_launch_owner_id(db: Session) -> int:
    admin = _admin(db)
    return int(admin.id) if admin else 1


def _early_launch_store(db: Session) -> tuple[int, dict]:
    """Persisted early-launch ledger: unique real founding Pro users only."""
    owner_id = _early_launch_owner_id(db)
    data = _feature(db, owner_id, "early-launch")
    if not data:
        data = {
            "total": EARLY_LAUNCH_TOTAL,
            "seedClaimed": 0,
            "seedClaimedThisWeek": 0,
            "realOnly": True,
            "claims": [],
        }
        _save_feature(db, owner_id, "early-launch", "Early launch counter", data)
    # One-time wipe of old design seed (63 claimed / 12 this week) so the UI
    # only counts real users who received the founding Pro offer.
    if not data.get("realOnly"):
        data = {
            **data,
            "total": EARLY_LAUNCH_TOTAL,
            "seedClaimed": 0,
            "seedClaimedThisWeek": 0,
            "realOnly": True,
            "claims": list(data.get("claims") or []),
        }
        _save_feature(db, owner_id, "early-launch", "Early launch counter", data)
    data.setdefault("total", EARLY_LAUNCH_TOTAL)
    data["seedClaimed"] = 0
    data["seedClaimedThisWeek"] = 0
    data.setdefault("claims", [])
    return owner_id, data


def _founding_checkout_claims(db: Session) -> list[dict]:
    """Backfill candidates from billing-checkout rows marked founding."""
    rows = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.kind == "billing-checkout")
        .order_by(FeatureRecord.id.asc())
        .all()
    )
    out: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        payload = dict(row.payload or {})
        if not bool(payload.get("founding")):
            continue
        uid = int(row.customer_id)
        if uid in seen:
            continue
        at = _parse_iso(payload.get("at")) or getattr(row, "created_at", None) or datetime.utcnow()
        seen.add(uid)
        out.append(
            {
                "userId": uid,
                "planId": str(payload.get("planId") or "pro"),
                "at": at.isoformat() + "Z",
                "source": "checkout",
            }
        )
    return out


def _sync_early_launch_claims(db: Session) -> dict:
    """Merge founding checkouts into the early-launch ledger (unique by user)."""
    owner_id, data = _early_launch_store(db)
    claims = list(data.get("claims") or [])
    by_user = {int(c.get("userId")): dict(c) for c in claims if c.get("userId") is not None}
    changed = False
    for item in _founding_checkout_claims(db):
        uid = int(item["userId"])
        if uid not in by_user:
            by_user[uid] = item
            changed = True
        else:
            prev_at = _parse_iso(by_user[uid].get("at"))
            next_at = _parse_iso(item.get("at"))
            if next_at and (prev_at is None or next_at > prev_at):
                by_user[uid] = {**by_user[uid], **item}
                changed = True
    if changed:
        ordered = sorted(by_user.values(), key=lambda c: _parse_iso(c.get("at")) or datetime.min)
        data = {**data, "claims": ordered[-500:]}
        _save_feature(db, owner_id, "early-launch", "Early launch counter", data)
    return data


def _user_has_early_launch_claim(data: dict, user_id: int) -> bool:
    uid = int(user_id)
    return any(int(c.get("userId") or 0) == uid for c in (data.get("claims") or []))


def _record_early_launch_claim(db: Session, user_id: int, plan_id: str) -> dict:
    """Record one early-launch offer received (idempotent per user, hard cap 100)."""
    owner_id, data = _early_launch_store(db)
    claims = list(data.get("claims") or [])
    uid = int(user_id)
    if any(int(c.get("userId") or 0) == uid for c in claims):
        return data
    total = max(1, int(data.get("total") or EARLY_LAUNCH_TOTAL))
    if len(claims) >= total:
        return data
    claims.append(
        {
            "userId": uid,
            "planId": str(plan_id or "pro"),
            "at": datetime.utcnow().isoformat() + "Z",
            "source": "offer",
        }
    )
    data = {**data, "claims": claims[-500:], "seedClaimed": 0, "seedClaimedThisWeek": 0, "realOnly": True}
    _save_feature(db, owner_id, "early-launch", "Early launch counter", data)
    return data


def _early_launch_payload(db: Session) -> dict:
    """Live early-launch counter: unique founding Pro users / 100 spots."""
    data = _sync_early_launch_claims(db)
    total = max(1, min(EARLY_LAUNCH_TOTAL, int(data.get("total") or EARLY_LAUNCH_TOTAL)))
    claims = list(data.get("claims") or [])
    # Deduplicate by userId in case older ledgers double-counted.
    by_user: dict[int, dict] = {}
    for c in claims:
        if c.get("userId") is None:
            continue
        uid = int(c["userId"])
        prev = by_user.get(uid)
        if prev is None:
            by_user[uid] = dict(c)
            continue
        prev_at = _parse_iso(prev.get("at"))
        next_at = _parse_iso(c.get("at"))
        if next_at and (prev_at is None or next_at > prev_at):
            by_user[uid] = dict(c)
    unique = list(by_user.values())
    times: list[datetime] = []
    for c in unique:
        at = _parse_iso(c.get("at"))
        if at:
            times.append(at)
    times.sort()
    live_count = len(unique)
    claimed = min(total, live_count)
    week_ago = datetime.utcnow() - timedelta(days=7)
    claimed_week = sum(1 for t in times if t >= week_ago)
    if live_count and times:
        last_label = _ago_label(times[-1])
        last_at = times[-1].isoformat() + "Z"
    else:
        last_label = "No claims yet"
        last_at = None
    left = max(0, total - claimed)
    offer_open = left > 0
    return {
        "total": total,
        "claimed": claimed,
        "left": left,
        "offersReceived": live_count,
        "claimedThisWeek": claimed_week,
        "lastClaimed": last_label,
        "lastClaimedAt": last_at,
        "spotsHeadline": f"{left} of {total} spots left",
        "claimedLabel": f"{claimed} CLAIMED",
        "totalLabel": f"{total} SPOTS TOTAL",
        "proListMo": int(EARLY_LAUNCH_SEED["proListMo"]),
        "proLaunchMo": int(EARLY_LAUNCH_SEED["proLaunchMo"]),
        "offerOpen": offer_open,
        "live": True,
        "liveClaims": live_count,
        "seedClaimed": 0,
    }


@router.get("/early-launch")
def early_launch(db: Session = Depends(get_db)):
    """Lightweight live counter for the Pricing early-launch panel."""
    return {"ok": True, "live": True, **_early_launch_payload(db)}


@router.get("/catalog")
def catalog(db: Session = Depends(get_db)):
    early = _early_launch_payload(db)
    offer_open = bool(early.get("offerOpen"))
    plans = []
    for raw in CATALOG:
        plan = dict(raw)
        if plan["id"] == "pro":
            # While spots remain, Pro shows launch $79; after 100 claims, list $99.
            if offer_open:
                plan["mo"] = int(EARLY_LAUNCH_SEED["proLaunchMo"])
                plan["listMo"] = int(EARLY_LAUNCH_SEED["proListMo"])
                plan["founding"] = True
            else:
                plan["mo"] = int(EARLY_LAUNCH_SEED["proListMo"])
                plan["listMo"] = int(EARLY_LAUNCH_SEED["proListMo"])
                plan["founding"] = False
        plans.append(plan)
    return {
        "ok": True,
        "live": True,
        "plans": plans,
        "trialDays": 30,
        "earlyLaunch": early,
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
    early = _early_launch_payload(db)
    already_founding = _user_has_early_launch_claim(
        _early_launch_store(db)[1], user.id
    ) or bool(_feature(db, user.id, "billing-checkout").get("founding"))
    # Lock early-launch Pro price while spots remain (max 100 unique users).
    # Existing founding subscribers keep the offer even after the cap fills.
    founding = (plan["id"] == "pro") and (already_founding or int(early.get("left") or 0) > 0)
    if founding and plan["id"] == "pro":
        # Persist launch price on the checkout row.
        plan = {**plan, "mo": int(EARLY_LAUNCH_SEED["proLaunchMo"]), "listMo": int(EARLY_LAUNCH_SEED["proListMo"]), "founding": True}
    elif plan["id"] == "pro":
        plan = {**plan, "mo": int(EARLY_LAUNCH_SEED["proListMo"]), "listMo": int(EARLY_LAUNCH_SEED["proListMo"]), "founding": False}
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
            "founding": founding,
            "provider": "internal",
            "at": datetime.utcnow().isoformat() + "Z",
        },
    )
    if founding:
        _record_early_launch_claim(db, user.id, plan["id"])
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
        "founding": founding,
        "earlyLaunch": _early_launch_payload(db),
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
