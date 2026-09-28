"""Background auto-sync for connected Google accounts (GSC/GA4/PageSpeed/Places)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app import config
from app.database import SessionLocal
from app.models import FeatureRecord, GoogleConnection, OpsNotice

log = logging.getLogger("searchify.auto_sync")


def _scan_controls(db, user_id: int) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "workspace-controls")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    payload = (row.payload if row else None) or {}
    calls = payload.get("apiCalls") or {}
    rates = {"gsc": 0.02, "ga4": 0.02, "pagespeed": 0.04, "places": 0.03, "ads": 0.02, "openai": 0.08}
    spend = sum(int(calls.get(k) or 0) * rates[k] for k in rates)
    cap = float(payload.get("costCapUsd") or 0)
    return {
        "scanHours": float(payload.get("scanHours") or config.GOOGLE_AUTO_SYNC_HOURS or 6),
        "scansPaused": bool(payload.get("scansPaused")) or (cap > 0 and spend >= cap),
    }


def connections_due(db, max_age_hours: float) -> list[GoogleConnection]:
    rows = (
        db.query(GoogleConnection)
        .filter(GoogleConnection.refresh_token.isnot(None))
        .filter(GoogleConnection.refresh_token != "")
        .all()
    )
    due = []
    for row in rows:
        meta = row.meta or {}
        if meta.get("autoSync") is False:
            continue
        controls = _scan_controls(db, row.customer_id)
        if controls["scansPaused"]:
            continue
        hours = max(1.0, float(controls["scanHours"] or max_age_hours))
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        if row.last_sync_at is None or row.last_sync_at < cutoff:
            due.append(row)
    return due


def run_due_syncs() -> dict:
    from app.routers.google_connect import sync_user_google

    hours = float(config.GOOGLE_AUTO_SYNC_HOURS or 6)
    ok = fail = 0
    db = SessionLocal()
    try:
        due = connections_due(db, hours)
        for row in due:
            try:
                sync_user_google(db, row.customer_id)
                ok += 1
                log.info("auto-sync ok user=%s", row.customer_id)
            except Exception as exc:  # noqa: BLE001
                fail += 1
                row.last_error = str(exc)[:500]
                row.status = "error"
                db.add(
                    OpsNotice(
                        customer_id=row.customer_id,
                        kind="sync",
                        title="Scheduled Google sync failed",
                        detail=str(exc)[:500],
                    )
                )
                db.commit()
                log.warning("auto-sync fail user=%s err=%s", row.customer_id, exc)
    finally:
        db.close()
    return {"ok": ok, "fail": fail, "checked": ok + fail}


async def auto_sync_loop(stop: asyncio.Event):
    # First pass after short delay so API is ready
    interval = max(300, int(float(config.GOOGLE_AUTO_SYNC_HOURS or 6) * 3600))
    await asyncio.sleep(45)
    while not stop.is_set():
        if config.GOOGLE_AUTO_SYNC_ENABLED:
            try:
                await asyncio.to_thread(run_due_syncs)
            except Exception as exc:  # noqa: BLE001
                log.exception("auto-sync loop error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
