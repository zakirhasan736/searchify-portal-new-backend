"""HQ actions change real records: payments, commissions clearing, payouts, partner rates, problem emails."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import mailer
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.routers import partners as P
from app.security import get_current_user, hash_password
from tests.db_fixtures import drop_user


def _user(role, **kw):
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(username=f"hq_{tag}", email=f"hq_{tag}@example.test", password_hash=hash_password("x-pass-123"), role=role, plan=kw.get("plan", "starter"), site_limit=5)
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u


@pytest.fixture
def env(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject)) or False)
    admin = _user("ROLE_ADMIN")
    partner = _user("ROLE_PARTNER")
    customer = _user("ROLE_CLIENT", plan="growth")
    with SessionLocal() as db:
        P._save_feature(db, partner.id, "partner-profile", "Partner profile", {"handle": f"h{partner.id}", "rate": 0.3, "displayName": "Pat"})
        P._save_feature(db, partner.id, "partner-ledger", "Partner ledger", P._empty_ledger())
        P._save_feature(db, partner.id, "partner-payout", "Partner payout", {"method": "paypal", "email": partner.email, "legalName": "Pat LLC", "certified": True})
        P._save_feature(db, customer.id, "referred-by", "Referred by", {"partnerId": partner.id, "at": datetime.utcnow().isoformat() + "Z"})
    app.dependency_overrides[get_current_user] = lambda: admin
    yield {"c": TestClient(app), "admin": admin, "partner": partner, "customer": customer, "sent": sent}
    app.dependency_overrides.pop(get_current_user, None)
    with SessionLocal() as db:
        for u in (admin, partner, customer):
            db.execute(text("DELETE FROM projects WHERE customer_id = :u"), {"u": u.id})
        db.commit()
    for u in (admin, partner, customer):
        drop_user(u.id)


def test_no_invented_payment_history(env):
    c = env["c"]
    r = c.put(f"/api/v1/hq/customers/{env['customer'].id}", json={"action": "change_plan", "plan": "pro"})
    assert r.status_code == 200
    assert r.json()["customer"].get("payments") in ([], None)  # plan change is not a payment


def test_recorded_payment_creates_commission_that_clears_and_pays_out(env):
    c, partner = env["c"], env["partner"]
    r = c.put(f"/api/v1/hq/customers/{env['customer'].id}", json={"action": "record_payment", "amount": 200})
    assert r.status_code == 200 and "commission $60.00" in r.json()["message"]
    pays = r.json()["customer"]["payments"]
    assert pays and pays[0]["amount"] == 200 and pays[0]["status"] == "Paid"
    with SessionLocal() as db:
        ledger = P._feature(db, partner.id, "partner-ledger")
        assert ledger["totals"]["hold"] == 60.0
        # 31 days later the hold has passed.
        later = P.mature(P._feature(db, partner.id, "partner-ledger"), datetime.utcnow() + timedelta(days=31))
        assert later["totals"]["cleared"] == 60.0 and later["totals"]["hold"] == 0
        # Store the matured state, as a month would.
        P._save_feature(db, partner.id, "partner-ledger", "Partner ledger", later)
    key = f"u{partner.id}"
    assert c.put("/api/v1/hq/partners", json={"run": {key: "approved"}}).status_code == 200
    out = c.post("/api/v1/hq/partners/payout", json={}).json()
    assert "Recorded $60.00 paid to 1 partner" in out["message"]
    with SessionLocal() as db:
        ledger = P._feature(db, partner.id, "partner-ledger")
    assert ledger["totals"]["paid"] == 60.0 and ledger["totals"]["cleared"] == 0
    assert ledger["payouts"][-1][1] == 60.0
    assert ledger["earnings"][0][3] == "paid"


def test_rate_change_reaches_the_partner_account(env):
    c, partner = env["c"], env["partner"]
    assert c.put("/api/v1/hq/partners", json={"rates": {f"u{partner.id}": 35}}).status_code == 200
    with SessionLocal() as db:
        assert P._feature(db, partner.id, "partner-profile")["rate"] == 0.35


def test_problem_email_is_honest_without_smtp(env):
    c = env["c"]
    c.put(f"/api/v1/hq/customers/{env['customer'].id}", json={"action": "mark_failed"})
    items = c.get("/api/v1/hq/problems").json()["items"]
    item = next(i for i in items if i["id"] == f"bill-{env['customer'].id}")
    r = c.put(f"/api/v1/hq/problems/{item['id']}", json={"action": "email"}).json()
    assert "nothing was sent" in r["message"]
    assert not r["emailed"].get(item["id"])
    assert env["sent"] and env["sent"][-1][0] == env["customer"].email


def test_cap_table_is_validated_and_saved(env):
    c = env["c"]
    bad = c.put("/api/v1/hq/shareholders", json={"holders": [{"name": "A", "pct": 0.7}, {"name": "B", "pct": 0.5}]})
    assert bad.status_code == 400
    ok = c.put("/api/v1/hq/shareholders", json={"holders": [{"name": "Founder", "pct": 0.8, "shares": 8000, "where": "US", "wh": 0, "note": ""}]})
    assert ok.status_code == 200
    assert ok.json()["holders"][0]["name"] == "Founder"
    assert c.get("/api/v1/hq/shareholders").json()["holders"][0]["pct"] == 0.8
