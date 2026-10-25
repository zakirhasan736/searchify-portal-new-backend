"""Phone alerts. A tap opens a card. Nothing here publishes or retries a publish."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_SUBJECT
from app.models import FeatureRecord, PushDelivery, PushSubscription, PushVapid

log = logging.getLogger("searchify.push")


def _num(value) -> float:
    try:
        return float(str(value or "0").replace("%", "").replace(",", ""))
    except ValueError:
        return 0.0


def week_pair(daily) -> dict | None:
    """Last seven stored Search Console days beside the seven days before them."""
    rows = []
    for row in daily or []:
        if not row or not row[0]:
            continue
        rows.append({
            "date": str(row[0])[:10],
            "clicks": _num(row[1] if len(row) > 1 else 0),
            "impressions": _num(row[2] if len(row) > 2 else 0),
            "position": _num(row[4] if len(row) > 4 else 0),
        })
    rows.sort(key=lambda item: item["date"])
    if len(rows) < 8:
        return None
    end = datetime.strptime(rows[-1]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    this_start = end - timedelta(days=6)
    prev_end = this_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    def between(item, start, stop) -> bool:
        stamp = datetime.strptime(item["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return start <= stamp <= stop

    def total(items) -> dict:
        clicks = int(sum(item["clicks"] for item in items))
        impressions = int(sum(item["impressions"] for item in items))
        weighted = sum(item["position"] * item["impressions"] for item in items)
        position = round(weighted / impressions, 1) if impressions else None
        return {"clicks": clicks, "impressions": impressions, "position": position}

    return {
        "current": total([item for item in rows if between(item, this_start, end)]),
        "previous": total([item for item in rows if between(item, prev_start, prev_end)]),
    }


def issue_counts(issues) -> dict:
    errors = warnings = notices = 0
    for issue in issues or []:
        level = str((issue or {}).get("severity") or "Notice")
        if level in {"Critical", "Error"}:
            errors += 1
        elif level == "Warning":
            warnings += 1
        else:
            notices += 1
    return {"errors": errors, "warnings": warnings, "notices": notices}


def waiting_message(*, change_id: int, name: str, page: str, level: str = "Warning") -> dict | None:
    if str(level) in {"Notice", "notice"}:
        return None
    label = "Error" if level in {"Critical", "Error"} else "Warning"
    where = (page or name or "A page").strip()
    return {
        "title": "A fix is waiting",
        "body": f"{label}. {where}. Open the card to approve it or do it yourself. This alert does not publish.",
        "url": f"/app/queue/{change_id}",
        "tag": f"waiting-{change_id}",
    }


def failed_message(*, change_id: int, page: str) -> dict:
    where = (page or "This page").strip()
    return {
        "title": "Publish failed",
        "body": f"Error. {where}. Nothing went live. Open the card. This alert does not retry the publish.",
        "url": f"/app/queue/{change_id}",
        "tag": f"failed-{change_id}",
    }


def weekly_message(*, current: dict, previous: dict, errors: int, warnings: int, notices: int) -> dict:
    def shown(value) -> str:
        return "—" if value is None else str(value)

    body = (
        f"Clicks {current['clicks']} this week, {previous['clicks']} last week. "
        f"Impressions {current['impressions']} this week, {previous['impressions']} last week. "
        f"Average position {shown(current['position'])} this week, {shown(previous['position'])} last week. "
        f"Open: {errors} errors, {warnings} warnings, {notices} notices."
    )
    return {
        "title": "Weekly Searchify report",
        "body": body,
        "url": "/app",
        "tag": "weekly",
    }


def _latest(db: Session, user_id: int, kind: str) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict(row.payload or {}) if row else {}


def weekly_from_stored(db: Session, user_id: int) -> dict | None:
    organic = _latest(db, user_id, "organic-search")
    pair = week_pair(organic.get("daily"))
    if not pair:
        return None
    audit = _latest(db, user_id, "site-audit")
    counts = issue_counts(audit.get("issues"))
    return weekly_message(
        current=pair["current"],
        previous=pair["previous"],
        errors=counts["errors"],
        warnings=counts["warnings"],
        notices=counts["notices"],
    )


def _generate_keys() -> tuple[str, str]:
    import base64

    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid01

    vapid = Vapid01()
    vapid.generate_keys()
    private = vapid.private_pem().decode()
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return public, private


def vapid_keys(db: Session) -> tuple[str, str]:
    if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY:
        return VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY
    row = db.query(PushVapid).order_by(PushVapid.id.asc()).first()
    if row and row.public_key and row.private_key:
        return row.public_key, row.private_key
    public, private = _generate_keys()
    db.add(PushVapid(public_key=public, private_key=private))
    db.commit()
    return public, private


def deliver(db: Session, user_id: int, message: dict | None, *, kind: str, ref: str) -> dict:
    if not message:
        return {"sent": 0, "reason": "suppressed"}
    already = (
        db.query(PushDelivery)
        .filter(PushDelivery.customer_id == user_id, PushDelivery.kind == kind, PushDelivery.ref == ref)
        .first()
    )
    if already:
        return {"sent": 0, "reason": "already_sent"}
    subs = db.query(PushSubscription).filter(PushSubscription.customer_id == user_id).all()
    if not subs:
        return {"sent": 0, "reason": "no_subscription"}
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return {"sent": 0, "reason": "push_library_missing"}
    try:
        _public, private = vapid_keys(db)
    except Exception:  # noqa: BLE001
        log.exception("push_keys_failed")
        return {"sent": 0, "reason": "keys_unavailable"}
    sent = 0
    for sub in list(subs):
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps(message),
                vapid_private_key=private,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                db.delete(sub)
            else:
                log.warning("push_send_failed", extra={"status": status, "user_id": user_id})
        except Exception:  # noqa: BLE001
            log.exception("push_send_failed")
    if sent:
        db.add(PushDelivery(customer_id=user_id, kind=kind, ref=ref[:80]))
    db.commit()
    return {"sent": sent, "reason": "sent" if sent else "not_delivered"}


def notify_waiting(db: Session, user_id: int, change) -> dict:
    try:
        if getattr(change, "status", "") != "awaiting_approval":
            return {"sent": 0, "reason": "not_waiting"}
        message = waiting_message(
            change_id=change.id,
            name=str(getattr(change, "opportunity", "") or ""),
            page=str(getattr(change, "target_url", "") or ""),
            level="Warning",
        )
        return deliver(db, user_id, message, kind="waiting", ref=str(change.id))
    except Exception:  # noqa: BLE001
        log.exception("push_waiting_failed")
        return {"sent": 0, "reason": "error"}


def notify_failed(db: Session, user_id: int, change) -> dict:
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        message = failed_message(change_id=change.id, page=str(getattr(change, "target_url", "") or ""))
        return deliver(db, user_id, message, kind="failed", ref=f"{change.id}-{stamp}")
    except Exception:  # noqa: BLE001
        log.exception("push_failed_alert_failed")
        return {"sent": 0, "reason": "error"}


def notify_weekly(db: Session, user_id: int) -> dict:
    try:
        message = weekly_from_stored(db, user_id)
        if not message:
            return {"sent": 0, "reason": "need_14_days"}
        week = datetime.now(timezone.utc).strftime("%G-W%V")
        return deliver(db, user_id, message, kind="weekly", ref=week)
    except Exception:  # noqa: BLE001
        log.exception("push_weekly_failed")
        return {"sent": 0, "reason": "error"}
