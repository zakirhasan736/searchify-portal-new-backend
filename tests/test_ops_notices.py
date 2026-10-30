"""Command centre notices: one per problem, closed after a good sync; scheduled sync keeps the verified property."""

from datetime import datetime, timedelta

from sqlalchemy import text

from app import ops_notices
from app.database import SessionLocal
from app.models import CmsConnection, GoogleConnection, OpsNotice
from app.routers.google_connect import _apply_site_google
from tests.db_fixtures import drop_user, make_user

ERR = "This Google account cannot read Search Console data for sc-domain:acme.example (Domain property)."


def _cleanup(uid):
    with SessionLocal() as db:
        db.execute(text("DELETE FROM ops_notices WHERE customer_id = :u"), {"u": uid})
        db.commit()
    drop_user(uid)


def test_open_notice_dedupes_and_resolve_closes():
    user = make_user()
    try:
        with SessionLocal() as db:
            for _ in range(5):
                ops_notices.open_notice(db, user.id, "sync", "Scheduled Google sync failed", ERR)
                db.commit()
            rows = db.query(OpsNotice).filter(OpsNotice.customer_id == user.id, OpsNotice.status == "open").all()
            assert len(rows) == 1
            assert ops_notices.resolve(db, user.id, ("sync",)) == 1
            db.commit()
            assert db.query(OpsNotice).filter(OpsNotice.customer_id == user.id, OpsNotice.status == "open").count() == 0
    finally:
        _cleanup(user.id)


def test_tidy_closes_stale_sync_and_duplicates():
    user = make_user()
    try:
        with SessionLocal() as db:
            old = datetime.utcnow() - timedelta(days=2)
            for _ in range(8):
                db.add(OpsNotice(customer_id=user.id, kind="sync", title="Scheduled Google sync failed", detail=ERR, created_at=old))
            db.add(OpsNotice(customer_id=user.id, kind="scan", title="Scan blocked", detail="x"))
            db.add(OpsNotice(customer_id=user.id, kind="scan", title="Scan blocked", detail="x"))
            db.add(GoogleConnection(customer_id=user.id, status="connected", last_error="", last_sync_at=datetime.utcnow(), refresh_token="r"))
            db.commit()
            ops_notices.tidy(db, user.id)
            db.commit()
            left = db.query(OpsNotice).filter(OpsNotice.customer_id == user.id, OpsNotice.status == "open").all()
            assert [(n.kind) for n in left] == ["scan"]
    finally:
        _cleanup(user.id)


def test_tidy_keeps_one_when_google_still_failing():
    user = make_user()
    try:
        with SessionLocal() as db:
            for _ in range(3):
                db.add(OpsNotice(customer_id=user.id, kind="sync", title="Scheduled Google sync failed", detail=ERR))
            db.add(GoogleConnection(customer_id=user.id, status="error", last_error=ERR, refresh_token="r"))
            db.commit()
            ops_notices.tidy(db, user.id)
            db.commit()
            assert db.query(OpsNotice).filter(OpsNotice.customer_id == user.id, OpsNotice.status == "open").count() == 1
    finally:
        _cleanup(user.id)


def test_scheduled_sync_keeps_verified_property_over_stale_site_choice():
    user = make_user()
    try:
        with SessionLocal() as db:
            cms = CmsConnection(customer_id=user.id, provider="wordpress", site_url="https://acme.example", credentials={}, status="connected",
                                meta={"gscSiteUrl": "sc-domain:acme.example"})
            row = GoogleConnection(customer_id=user.id, status="connected", refresh_token="r", gsc_site_url="https://acme.example/",
                                   meta={"validated": {"gsc": {"value": "https://acme.example/", "at": "2026-10-30"}}})
            db.add_all([cms, row])
            db.commit()
            _apply_site_google(db, user.id, row, None)
            assert row.gsc_site_url == "https://acme.example/"
            assert cms.meta["gscSiteUrl"] == "https://acme.example/"  # repaired for next time
            # Another site's property is never kept: the site's own choice wins.
            row.gsc_site_url = "https://other.example/"
            row.meta = {"validated": {"gsc": {"value": "https://other.example/"}}}
            _apply_site_google(db, user.id, row, None)
            assert row.gsc_site_url == "https://acme.example/"
            db.rollback()
    finally:
        _cleanup(user.id)
