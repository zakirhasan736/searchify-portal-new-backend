"""Partner program end to end: apply → HQ approve → set password → sign in → referral tracking."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import User
from app.routers import partners as partners_router
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user


def _token_from(url: str, key: str) -> str:
    return parse_qs(urlparse(url).query)[key][0]


@pytest.fixture
def env(monkeypatch):
    admin = make_user("hqadmin", role="ROLE_ADMIN", plan="scale", site_limit=15)
    sent = []
    monkeypatch.setattr(partners_router.mailer, "send", lambda to, subject, body: sent.append((to, subject, body)) or False)
    created: list[str] = []
    yield {"client": TestClient(app), "admin": admin, "sent": sent, "created": created}
    app.dependency_overrides.pop(get_current_user, None)
    from sqlalchemy import text

    with SessionLocal() as db:
        # Remove this test's applications from the shared HQ inbox (the dev database is shared).
        for owner in db.query(User).filter(User.role == "ROLE_ADMIN").all():
            inbox = partners_router._feature(db, owner.id, "partner-applications")
            rows = [r for r in (inbox.get("rows") or []) if r.get("email") not in created]
            if inbox and len(rows) != len(inbox.get("rows") or []):
                partners_router._save_feature(db, owner.id, "partner-applications", "Partner applications", {"rows": rows})
        ids = [row.id for row in db.query(User).filter(User.email.in_(created)).all()] + [admin.id]
        for uid in ids:
            db.execute(text("DELETE FROM projects WHERE customer_id = :u"), {"u": uid})
        db.commit()
    for uid in ids:
        drop_user(uid)


def _as_admin(env):
    with SessionLocal() as db:
        admin = db.get(User, env["admin"].id)
        db.expunge(admin)
    app.dependency_overrides[get_current_user] = lambda: admin


def _hq_apps(env):
    _as_admin(env)
    return env["client"].get("/api/v1/hq/partners").json()["applications"]


def test_public_signup_can_never_create_an_admin(env):
    email = f"sneaky_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(email)
    r = env["client"].post("/api/v1/auth/signup", json={"username": email.split("@")[0], "email": email, "password": "Passw0rd!x", "roles": ["ROLE_ADMIN"]})
    assert r.status_code == 200
    assert r.json()["userType"] == "client"
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == email).one().role == "ROLE_CLIENT"


def test_apply_approve_set_password_and_sign_in(env):
    c = env["client"]
    email = f"maria_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(email)
    assert c.post("/api/v1/partners/apply", json={"name": "Maria Vega", "email": email, "channel": "YouTube", "agreed": True}).status_code == 200
    # Applying twice does not create a second pending application.
    c.post("/api/v1/partners/apply", json={"name": "Maria Vega", "email": email, "channel": "YouTube", "agreed": True})
    apps = [a for a in _hq_apps(env) if a["email"] == email]
    assert len(apps) == 1 and apps[0]["status"] == "pending" and apps[0]["id"].startswith("app-")

    # Cannot sign in before approval: no account exists.
    assert c.post("/api/v1/auth/signin", json={"username": email, "password": "anything"}).status_code == 401

    r = c.post(f"/api/v1/hq/partners/applications/{apps[0]['id']}/approve", json={"rate": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["emailed"] is False  # SMTP not configured in tests: the admin gets the link instead
    assert "/partners/login?setup=" in body["setupUrl"]
    assert "?ref=" in body["referralLink"]
    assert env["sent"] and env["sent"][-1][0] == email
    assert [a for a in _hq_apps(env) if a["email"] == email][0]["status"] == "approved"
    real = [p for p in env["client"].get("/api/v1/hq/partners").json()["partners"] if p.get("email") == email]
    assert real and real[0]["rate"] == 30

    app.dependency_overrides.pop(get_current_user, None)
    token = _token_from(body["setupUrl"], "setup")
    assert c.post("/api/v1/partners/set-password", json={"token": token, "password": "short"}).status_code == 400
    done = c.post("/api/v1/partners/set-password", json={"token": token, "password": "Partner-Pass-1"})
    assert done.status_code == 200 and done.json()["userType"] == "partner"
    # The link is single-use: it stops working once the password changed.
    assert c.post("/api/v1/partners/set-password", json={"token": token, "password": "Another-Pass-2"}).status_code == 400

    signin = c.post("/api/v1/auth/signin", json={"username": email, "password": "Partner-Pass-1"})
    assert signin.status_code == 200 and signin.json()["userType"] == "partner"
    me = c.get("/api/v1/partners/me", headers={"Authorization": f"Bearer {signin.json()['token']}"})
    assert me.status_code == 200 and me.json()["profile"]["rate"] == 0.3


def test_decline_and_reset_password(env):
    c = env["client"]
    email = f"ben_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(email)
    c.post("/api/v1/partners/apply", json={"name": "Ben Carter", "email": email, "channel": "Coupons", "agreed": True})
    app_id = [a for a in _hq_apps(env) if a["email"] == email][0]["id"]
    assert c.post(f"/api/v1/hq/partners/applications/{app_id}/decline").json()["ok"] is True
    assert [a for a in _hq_apps(env) if a["email"] == email][0]["status"] == "declined"
    assert c.post(f"/api/v1/hq/partners/applications/{app_id}/approve").status_code == 409
    app.dependency_overrides.pop(get_current_user, None)
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == email).one_or_none() is None

    # Reset for an approved partner sends a real 30-minute link.
    partner_email = f"pat_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(partner_email)
    c.post("/api/v1/partners/apply", json={"name": "Pat Lee", "email": partner_email, "agreed": True})
    pid = [a for a in _hq_apps(env) if a["email"] == partner_email][0]["id"]
    c.post(f"/api/v1/hq/partners/applications/{pid}/approve")
    app.dependency_overrides.pop(get_current_user, None)
    env["sent"].clear()
    assert c.post("/api/v1/partners/reset-password", json={"email": partner_email}).json()["ok"] is True
    link = next(line for line in env["sent"][-1][2].splitlines() if "/partners/login?reset=" in line)
    assert c.post("/api/v1/partners/set-password", json={"token": _token_from(link, "reset"), "password": "Reset-Pass-99"}).status_code == 200
    assert c.post("/api/v1/auth/signin", json={"username": partner_email, "password": "Reset-Pass-99"}).status_code == 200
    # Unknown emails get the same answer and no email.
    env["sent"].clear()
    assert c.post("/api/v1/partners/reset-password", json={"email": "nobody@example.test"}).json()["ok"] is True
    assert env["sent"] == []


def test_referral_click_and_signup_are_attributed(env):
    c = env["client"]
    email = f"ref_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(email)
    c.post("/api/v1/partners/apply", json={"name": "Ref Partner", "email": email, "agreed": True})
    app_id = [a for a in _hq_apps(env) if a["email"] == email][0]["id"]
    handle = c.post(f"/api/v1/hq/partners/applications/{app_id}/approve").json()["referralLink"].split("?ref=")[1]
    app.dependency_overrides.pop(get_current_user, None)

    assert c.post("/api/v1/partners/click", json={"ref": handle}).json()["ok"] is True
    assert c.post("/api/v1/partners/click", json={"ref": "no-such-partner"}).json()["ok"] is True
    customer = f"cust_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(customer)
    c.post("/api/v1/auth/signup", json={"username": customer.split("@")[0], "email": customer, "password": "Passw0rd!x", "ref": handle})

    with SessionLocal() as db:
        partner = db.query(User).filter(User.email == email).one()
        ledger = partners_router._feature(db, partner.id, "partner-ledger")
    assert ledger["clicks"][-1] == 1 and ledger["signups"][-1] == 1
    assert ledger["refs"][0][1] == "New customer"  # customer names stay private
    assert ledger["refSummary"].startswith("1 sign-up")


def test_plan_choice_and_payment_become_commission(env):
    c = env["client"]
    email = f"pay_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(email)
    c.post("/api/v1/partners/apply", json={"name": "Pay Partner", "email": email, "agreed": True})
    app_id = [a for a in _hq_apps(env) if a["email"] == email][0]["id"]
    handle = c.post(f"/api/v1/hq/partners/applications/{app_id}/approve").json()["referralLink"].split("?ref=")[1]
    app.dependency_overrides.pop(get_current_user, None)
    customer = f"buyer_{uuid.uuid4().hex[:8]}@example.test"
    env["created"].append(customer)
    signup = c.post("/api/v1/auth/signup", json={"username": customer.split("@")[0], "email": customer, "password": "Passw0rd!x", "ref": handle}).json()
    headers = {"Authorization": f"Bearer {signup['token']}"}
    assert c.post("/api/v1/billing/checkout", json={"planId": "pro"}, headers=headers).status_code == 200

    with SessionLocal() as db:
        partner = db.query(User).filter(User.email == email).one()
        buyer = db.query(User).filter(User.email == customer).one()
        assert partners_router._feature(db, partner.id, "partner-ledger")["refs"][0][2] == "Pro"
        commission = partners_router.record_payment(db, buyer, 79.0, "pro")
        ledger = partners_router._feature(db, partner.id, "partner-ledger")
    assert commission == 23.7  # 30% of $79
    assert ledger["refs"][0][3] == "paying"
    assert ledger["earnings"][0][3] == "hold" and ledger["earnings"][0][4].startswith("Clears ")
    assert ledger["totals"]["hold"] == 23.7 and ledger["mix"]["Pro"] == 1 and ledger["paying"][-1] == 1
