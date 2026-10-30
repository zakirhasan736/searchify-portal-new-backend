"""One open notice per problem: dedupe repeats and close notices once the problem is gone."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import GoogleConnection, OpsNotice


def open_notice(db: Session, user_id: int, kind: str, title: str, detail: str) -> OpsNotice:
    """Add a notice, or refresh the open one of the same kind instead of stacking duplicates."""
    existing = (
        db.query(OpsNotice)
        .filter(OpsNotice.customer_id == user_id, OpsNotice.kind == kind, OpsNotice.status == "open")
        .order_by(OpsNotice.id.desc())
        .first()
    )
    if existing is not None:
        existing.title = title[:200]
        existing.detail = detail[:500]
        existing.created_at = datetime.utcnow()  # "last seen"
        return existing
    row = OpsNotice(customer_id=user_id, kind=kind, title=title[:200], detail=detail[:500])
    db.add(row)
    return row


def resolve(db: Session, user_id: int, kinds: tuple[str, ...]) -> int:
    rows = (
        db.query(OpsNotice)
        .filter(OpsNotice.customer_id == user_id, OpsNotice.kind.in_(kinds), OpsNotice.status == "open")
        .all()
    )
    for row in rows:
        row.status = "resolved"
    return len(rows)


def tidy(db: Session, user_id: int) -> None:
    """Close stale Google sync notices and collapse duplicates left by older versions."""
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user_id).first()
    open_rows = (
        db.query(OpsNotice)
        .filter(OpsNotice.customer_id == user_id, OpsNotice.status == "open")
        .order_by(OpsNotice.id.desc())
        .all()
    )
    healthy = bool(google and google.status == "connected" and not google.last_error and google.last_sync_at)
    seen: set[tuple[str, str]] = set()
    for row in open_rows:
        if row.kind in {"sync", "retry"} and healthy and (row.created_at is None or row.created_at <= google.last_sync_at):
            row.status = "resolved"  # a later sync succeeded
            continue
        key = (row.kind, " ".join((row.detail or row.title or "").split())[:200])
        if key in seen:
            row.status = "resolved"  # duplicate of a newer open notice
            continue
        seen.add(key)
