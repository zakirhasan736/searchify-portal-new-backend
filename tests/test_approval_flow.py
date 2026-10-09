"""Approval queue against the real database. The CMS write and the live-page read are mocked."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CmsConnection, SiteChange
from app.routers import operator
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user

LIVE = {"title": "Old Title – Acme", "description": "Old description."}


@pytest.fixture
def env(monkeypatch):
    owner, other = make_user(), make_user()
    current = {"user": owner}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    live = {"value": dict(LIVE)}
    writes = []

    def fake_apply(provider, creds, site_url, change):
        writes.append(change)
        time.sleep(0.2)
        if not creds.get("applicationPassword"):
            return {"ok": True, "dryRun": True, "provider": provider, "detail": "Dry-run — add WordPress Application Password to execute live."}
        if creds.get("applicationPassword") == "fail":
            return {"ok": False, "dryRun": False, "provider": provider, "detail": "WordPress said 401"}
        verified = creds.get("applicationPassword") != "unverified"
        live["value"] = {"title": f"{change['title']} – Acme", "description": change["metaDescription"]}
        return {"ok": True, "dryRun": False, "provider": provider, "verified": verified, "remoteId": "7", "resource": "page",
                "before": {"title": "Old Title", "metaDescription": "Old description."}}

    monkeypatch.setattr(operator.cms_connectors, "apply_change", fake_apply)
    monkeypatch.setattr(operator, "_live_listing", lambda url: dict(live["value"]) if live["value"] is not None else None)

    with SessionLocal() as db:
        conn = CmsConnection(customer_id=owner.id, provider="wordpress", site_url="https://acme.example",
                             credentials={"username": "u", "applicationPassword": "secret"}, status="connected")
        db.add(conn)
        db.commit()
        conn_id = conn.id

    def new_change(**over) -> int:
        with SessionLocal() as db:
            row = SiteChange(
                customer_id=owner.id, cms_connection_id=conn_id, source="site_scan", target_url="https://acme.example/",
                change_type="meta", status="awaiting_approval",
                proposed={"title": "Plumber in Calgary | Same-Day Repairs", "metaDescription": "Calgary plumbing repairs, same day.",
                          "beforeTitle": LIVE["title"], "beforeDescription": LIVE["description"], **over},
            )
            db.add(row)
            db.commit()
            return row.id

    def set_password(value: str):
        with SessionLocal() as db:
            row = db.get(CmsConnection, conn_id)
            row.credentials = {"username": "u", "applicationPassword": value}
            db.commit()

    yield {"client": TestClient(app), "owner": owner, "other": other, "current": current, "live": live,
           "writes": writes, "new": new_change, "password": set_password}
    app.dependency_overrides.pop(get_current_user, None)
    drop_user(owner.id)
    drop_user(other.id)


def status_of(change_id: int) -> str:
    with SessionLocal() as db:
        return db.get(SiteChange, change_id).status


def test_publish_requires_approval(env):
    c, cid = env["client"], env["new"]()
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_approved"
    assert env["writes"] == []


def test_publishes_exactly_the_approved_edited_text(env):
    c, cid = env["client"], env["new"]()
    assert c.patch(f"/api/v1/operator/changes/{cid}", json={"title": "Edited Title For Calgary Plumbing", "metaDescription": "Edited description."}).status_code == 200
    assert c.post(f"/api/v1/operator/changes/{cid}/approve").status_code == 200
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 200, r.text
    assert env["writes"][-1]["title"] == "Edited Title For Calgary Plumbing"
    assert env["writes"][-1]["metaDescription"] == "Edited description."
    assert "beforeTitle" not in env["writes"][-1]
    assert status_of(cid) == "monitoring"
    assert r.json()["execution"]["liveCheck"]["titleLive"] is True


def test_edit_after_approval_withdraws_approval(env):
    c, cid = env["client"], env["new"]()
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    r = c.patch(f"/api/v1/operator/changes/{cid}", json={"title": "Sneaky change after approval here"})
    assert r.json()["approvalWithdrawn"] is True and r.json()["status"] == "awaiting_approval"
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 409
    assert env["writes"] == []


def test_rejected_change_never_publishes(env):
    c, cid = env["client"], env["new"]()
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    assert c.post(f"/api/v1/operator/changes/{cid}/dismiss").json()["status"] == "dismissed"
    assert c.post(f"/api/v1/operator/changes/{cid}/execute", json={}).status_code == 409
    assert c.post(f"/api/v1/operator/changes/{cid}/approve").status_code == 409
    assert env["writes"] == []


def test_source_change_sends_back_to_review(env):
    c, cid = env["client"], env["new"]()
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    env["live"]["value"] = {"title": "Someone edited this in WordPress", "description": LIVE["description"]}
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "source_changed"
    assert status_of(cid) == "awaiting_approval"
    with SessionLocal() as db:
        proposed = db.get(SiteChange, cid).proposed
    assert proposed["beforeTitle"] == "Someone edited this in WordPress"
    assert proposed["sourceChanged"]["fields"] == ["title"]
    assert env["writes"] == []


def test_unreadable_live_page_blocks_publish(env):
    c, cid = env["client"], env["new"]()
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    env["live"]["value"] = None
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "source_unreadable"
    assert env["writes"] == []


def test_no_double_publish_under_concurrent_clicks(env):
    c, cid = env["client"], env["new"]()
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    codes = []

    def click():
        codes.append(TestClient(app).post(f"/api/v1/operator/changes/{cid}/execute", json={}).status_code)

    threads = [threading.Thread(target=click) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes).count(200) == 1, codes
    assert len(env["writes"]) == 1
    assert c.post(f"/api/v1/operator/changes/{cid}/execute", json={}).json()["detail"]["code"] == "already_published"


def test_missing_credentials_is_not_reported_as_published(env):
    c, cid = env["client"], env["new"]()
    env["password"]("")
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_published"
    assert status_of(cid) == "approved"


def test_failed_publish_is_failed_and_can_retry(env):
    c, cid = env["client"], env["new"]()
    env["password"]("fail")
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    r = c.post(f"/api/v1/operator/changes/{cid}/execute", json={})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "publish_failed"
    assert status_of(cid) == "failed"
    env["password"]("secret")
    assert c.post(f"/api/v1/operator/changes/{cid}/execute", json={}).status_code == 200


def test_unverified_write_is_labelled(env):
    c, cid = env["client"], env["new"]()
    env["password"]("unverified")
    c.post(f"/api/v1/operator/changes/{cid}/approve")
    assert c.post(f"/api/v1/operator/changes/{cid}/execute", json={}).status_code == 200
    assert status_of(cid) == "published_unverified"


def test_cannot_approve_without_text(env):
    c, cid = env["client"], env["new"](title="", metaDescription="")
    r = c.post(f"/api/v1/operator/changes/{cid}/approve")
    assert r.status_code == 422


def test_other_tenant_cannot_touch_change(env):
    c, cid = env["client"], env["new"]()
    env["current"]["user"] = env["other"]
    for path in ("approve", "execute", "dismiss"):
        assert c.post(f"/api/v1/operator/changes/{cid}/{path}", json={}).status_code == 404
    assert c.patch(f"/api/v1/operator/changes/{cid}", json={"title": "x"}).status_code == 404
    env["current"]["user"] = env["owner"]
    assert status_of(cid) == "awaiting_approval"
