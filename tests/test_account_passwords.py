"""Forgot / reset / change password for customers, partners and admins."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import mailer
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.security import hash_password
from tests.db_fixtures import drop_user


def _make(role: str, password: str = "Start-Pass-1") -> User:
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        user = User(username=f"acct_{tag}", email=f"acct_{tag}@example.test", password_hash=hash_password(password), role=role, plan="starter", site_limit=5)
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


@pytest.fixture
def env(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    made: list[int] = []

    def make(role="ROLE_CLIENT", password="Start-Pass-1"):
        user = _make(role, password)
        made.append(user.id)
        return user

    yield {"c": TestClient(app), "sent": sent, "make": make}
    with SessionLocal() as db:
        for uid in made:
            db.execute(text("DELETE FROM projects WHERE customer_id = :u"), {"u": uid})
        db.commit()
    for uid in made:
        drop_user(uid)


def _link(sent, marker):
    body = sent[-1][2]
    line = next(line for line in body.splitlines() if marker in line)
    query = parse_qs(urlparse(line.strip()).query)
    return next(iter(query.values()))[0]


def _signin(c, user, password):
    return c.post("/api/v1/auth/signin", json={"username": user.email, "password": password})


def test_old_username_only_reset_is_closed(env):
    user = env["make"]()
    r = env["c"].post("/api/v1/auth/forgot-password", json={"username": user.username, "password": "Hijack-123"})
    assert r.status_code == 410
    assert _signin(env["c"], user, "Hijack-123").status_code == 401
    assert _signin(env["c"], user, "Start-Pass-1").status_code == 200


@pytest.mark.parametrize("role,area,marker", [
    ("ROLE_CLIENT", "customer", "/reset-password?token="),
    ("ROLE_PARTNER", "partner", "/partners/login?reset="),
    ("ROLE_ADMIN", "admin", "/admin/login?reset="),
])
def test_forgot_and_reset_for_each_area(env, role, area, marker):
    c, user = env["c"], env["make"](role)
    r = c.post("/api/v1/auth/forgot", json={"email": user.email, "area": area})
    assert r.status_code == 200 and "reset link" in r.json()["detail"]
    assert env["sent"][-1][0] == user.email
    token = _link(env["sent"], marker)
    # The link token is not a session.
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert c.post("/api/v1/auth/reset", json={"token": token, "password": "short"}).status_code == 400
    done = c.post("/api/v1/auth/reset", json={"token": token, "password": "Brand-New-Pass-9"})
    assert done.status_code == 200 and done.json()["area"] == area
    assert c.post("/api/v1/auth/reset", json={"token": token, "password": "Another-Pass-9"}).status_code == 400  # single use
    assert _signin(c, user, "Brand-New-Pass-9").status_code == 200


def test_forgot_only_sends_from_the_matching_sign_in_page(env):
    c, admin = env["c"], env["make"]("ROLE_ADMIN")
    env["sent"].clear()
    # An admin email typed on the customer page gets the same answer and no email.
    r = c.post("/api/v1/auth/forgot", json={"email": admin.email, "area": "customer"})
    assert r.status_code == 200 and env["sent"] == []
    assert c.post("/api/v1/auth/forgot", json={"email": "nobody@example.test", "area": "admin"}).json()["ok"] is True
    assert env["sent"] == []


def test_change_password_needs_current_and_signs_out_old_sessions(env):
    c, user = env["c"], env["make"]()
    old = _signin(c, user, "Start-Pass-1").json()["token"]
    auth = {"Authorization": f"Bearer {old}"}
    assert c.post("/api/v1/auth/change-password", json={"current": "wrong", "password": "Next-Pass-123"}, headers=auth).status_code == 400
    assert c.post("/api/v1/auth/change-password", json={"current": "Start-Pass-1", "password": "Start-Pass-1"}, headers=auth).status_code == 400
    r = c.post("/api/v1/auth/change-password", json={"current": "Start-Pass-1", "password": "Next-Pass-123"}, headers=auth)
    assert r.status_code == 200
    assert c.get("/api/v1/auth/me", headers=auth).status_code == 401  # old session signed out
    assert c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 200
    assert _signin(c, user, "Next-Pass-123").status_code == 200


def test_admin_password_changed_in_hq_survives_restart(env, monkeypatch):
    from app import config, seed

    email = f"envadmin_{uuid.uuid4().hex[:8]}@example.test"
    monkeypatch.setattr(config, "ADMIN_EMAIL", email)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "Env-Admin-Pass-1")
    with SessionLocal() as db:
        seed.ensure_admin_account(db)
        admin = db.query(User).filter(User.email == email).one()
        admin_id = admin.id
    try:
        c = env["c"]
        tok = c.post("/api/v1/auth/signin", json={"username": email, "password": "Env-Admin-Pass-1"}).json()["token"]
        assert c.post("/api/v1/auth/change-password", json={"current": "Env-Admin-Pass-1", "password": "Changed-In-HQ-2"}, headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        with SessionLocal() as db:
            seed.ensure_admin_account(db)  # app restart, same env value
        assert c.post("/api/v1/auth/signin", json={"username": email, "password": "Changed-In-HQ-2"}).status_code == 200
        monkeypatch.setattr(config, "ADMIN_PASSWORD", "Recovery-Pass-3")
        with SessionLocal() as db:
            seed.ensure_admin_account(db)  # env value changed: recovery
        assert c.post("/api/v1/auth/signin", json={"username": email, "password": "Recovery-Pass-3"}).status_code == 200
    finally:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM projects WHERE customer_id = :u"), {"u": admin_id})
            db.commit()
        drop_user(admin_id)
