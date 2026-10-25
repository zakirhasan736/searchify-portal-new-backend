"""Two partner programs (founding 35-40%, affiliate 20-25%), the automatic step-up, and the pitch writer."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.routers import partners as P
from app.security import get_current_user, hash_password
from tests.db_fixtures import drop_user


def _user(role):
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(username=f"pp_{tag}", email=f"pp_{tag}@example.test", password_hash=hash_password("x-pass-123"), role=role, plan="starter", site_limit=5)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u


@pytest.fixture
def partner(monkeypatch):
    monkeypatch.setattr(mailer, "send", lambda *a, **k: False)
    monkeypatch.setattr("app.openai_client.openai_configured", lambda: False)
    u = _user("ROLE_PARTNER")
    with SessionLocal() as db:
        P._save_feature(db, u.id, "partner-profile", "Partner profile", {"handle": f"pp{u.id}", "displayName": "Ava"})
    app.dependency_overrides[get_current_user] = lambda: u
    yield u
    app.dependency_overrides.pop(get_current_user, None)
    drop_user(u.id)


def test_programs_start_at_their_base_rate():
    assert P.rate_of({"tier": "standard"}) == 0.20
    assert P.rate_of({"tier": "founding"}) == 0.35
    status = P.program_status({"tier": "standard", "rate": 0.2})
    assert status["top"] == 0.25 and status["toGo"] == 10 and not status["steppedUp"]


def test_ten_paying_referrals_in_six_months_step_the_rate_up(partner):
    now = datetime.utcnow()
    with SessionLocal() as db:
        for i in range(9):
            P._count_qualified(db, partner, now - timedelta(days=i))
        assert P.rate_of(P._feature(db, partner.id, "partner-profile")) == 0.20
        P._count_qualified(db, partner, now)
        prof = P._feature(db, partner.id, "partner-profile")
    assert prof["rate"] == 0.25 and prof["rateUpAt"]
    assert P.program_status(prof)["steppedUp"]


def test_old_referrals_do_not_count_toward_the_step_up(partner):
    long_ago = datetime.utcnow() - timedelta(days=400)
    with SessionLocal() as db:
        for _ in range(10):
            P._count_qualified(db, partner, long_ago)
        prof = P._feature(db, partner.id, "partner-profile")
    assert P.program_status(prof)["qualified6m"] == 0


def test_writer_always_includes_link_and_disclosure(partner):
    c = TestClient(app)
    for channel in ("tiktok", "email", "instagram"):
        r = c.post("/api/v1/partners/write-message", json={"channel": channel, "audience": "local", "tone": "bold", "angle": "Great for plumbers"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert f"?ref=pp{partner.id}" in body["text"]
        assert "commission" in body["text"].lower() or "#ad" in body["text"].lower()
        assert "guarantee" not in body["text"].lower()
        assert body["source"] == "template"
    assert c.post("/api/v1/partners/write-message", json={"channel": "fax"}).status_code == 400


def test_writer_needs_a_partner_account(partner):
    customer = _user("ROLE_CLIENT")
    app.dependency_overrides[get_current_user] = lambda: customer
    try:
        assert TestClient(app).post("/api/v1/partners/write-message", json={"channel": "email"}).status_code == 403
    finally:
        drop_user(customer.id)
