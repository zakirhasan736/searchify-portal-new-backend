import pytest
from fastapi.testclient import TestClient

from app import site_profiles
from app.database import SessionLocal
from app.main import app
from app.models import FeatureRecord
from app.routers import operator
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user

ANSWERS = {
    "site": "https://www.acme-plumbing.example/",
    "businessType": "Services",
    "reach": "A local area",
    "market": "Calgary, Alberta",
    "shortGoal": "More booked repair calls",
    "industry": "No special category",
    "avoid": "guaranteed results",
}


def journey(answers=None, status="ready"):
    return {"planId": "starter", "billing": "monthly", "activeId": 1,
            "sites": [{"id": 1, "status": status, "answers": answers or ANSWERS, "createdAt": 1}]}


@pytest.fixture
def env():
    owner, other = make_user(), make_user()
    current = {"user": owner}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    yield {"client": TestClient(app), "owner": owner, "other": other, "current": current}
    app.dependency_overrides.pop(get_current_user, None)
    drop_user(owner.id)
    drop_user(other.id)


def test_answers_persist_on_the_server_and_hydrate(env):
    c = env["client"]
    assert c.get("/api/v1/operator/journey").json()["journey"] is None
    r = c.put("/api/v1/operator/journey", json={"state": journey()})
    assert r.status_code == 200
    profile = r.json()["profiles"][0]
    assert profile["host"] == "acme-plumbing.example"
    assert profile["location"]["iso"] == "CA" and profile["location"]["city"] == "Calgary"
    saved = c.get("/api/v1/operator/journey").json()["journey"]["state"]
    assert saved["sites"][0]["answers"]["market"] == "Calgary, Alberta"
    stored = c.get("/api/v1/operator/site-profile", params={"site": "acme-plumbing.example"}).json()["profile"]
    assert stored["brief"]["businessType"] == "Services" and stored["brief"]["goal"] == "More booked repair calls"


def test_profiles_are_per_tenant(env):
    c = env["client"]
    c.put("/api/v1/operator/journey", json={"state": journey()})
    env["current"]["user"] = env["other"]
    assert c.get("/api/v1/operator/journey").json()["journey"] is None
    assert c.get("/api/v1/operator/site-profile", params={"site": "acme-plumbing.example"}).json()["profile"] is None


def test_draft_sites_do_not_create_profiles(env):
    r = env["client"].put("/api/v1/operator/journey", json={"state": journey(status="draft")})
    assert r.json()["profiles"] == []


def test_unknown_market_is_flagged_not_defaulted(env):
    r = env["client"].put("/api/v1/operator/journey", json={"state": journey({**ANSWERS, "market": "Springfield"})})
    loc = r.json()["profiles"][0]["location"]
    assert loc["ok"] is False and "country" in loc["message"]


def test_changing_answers_marks_scan_stale(env):
    c, owner = env["client"], env["owner"]
    c.put("/api/v1/operator/journey", json={"state": journey()})
    with SessionLocal() as db:
        db.add(FeatureRecord(customer_id=owner.id, kind="site-scan", title="Site scan acme-plumbing.example",
                             payload={"scannedAt": "2099-01-01T00:00:00", "pages": [{"url": "x"}]}, status="stored"))
        db.commit()
    c.put("/api/v1/operator/journey", json={"state": journey({**ANSWERS, "shortGoal": "Same"})})
    c.put("/api/v1/operator/journey", json={"state": journey({**ANSWERS, "shortGoal": "Same"})})
    with SessionLocal() as db:
        scan = operator._latest_scan(db, owner.id, "acme-plumbing.example")
    assert scan["stale"] is True
    assert operator._scan_fresh(scan) is False


def test_stored_brief_wins_over_request_copy(env):
    env["client"].put("/api/v1/operator/journey", json={"state": journey()})
    with SessionLocal() as db:
        brief = operator._stored_brief(db, env["owner"], "https://acme-plumbing.example", {"market": "Toronto", "competitors": "rival.example"})
    assert brief["market"] == "Calgary, Alberta"
    assert brief["competitors"] == "rival.example"


def test_bad_payload_is_cleaned():
    state = site_profiles._clean_state({"planId": "hacker", "sites": [{"id": 5, "status": "weird", "answers": {"market": "x" * 900, "token": "secret"}}]})
    site = state["sites"][0]
    assert state["planId"] is None and site["status"] == "draft"
    assert "token" not in site["answers"] and len(site["answers"]["market"]) == 600


def test_conflicts_between_setup_and_site():
    scan = {
        "host": "acme.example",
        "pages": [{"url": "https://acme.example/", "title": "Plumbing in Edmonton", "text": "Edmonton plumbing repairs"},
                  *[{"url": f"https://acme.example/s{i}", "title": "Service", "text": "drain"} for i in range(3)]],
        "pageMap": {f"https://acme.example/s{i}": {"role": "service"} for i in range(3)},
        "business": {"places": ["Edmonton"]},
    }
    found = site_profiles.conflicts({"market": "Calgary, Alberta", "businessType": "Products", "competitors": "www.acme.example"}, scan)
    fields = {f["field"] for f in found}
    assert fields == {"market", "businessType", "competitors"}
    assert site_profiles.conflicts({"market": "Edmonton, Alberta", "businessType": "Services"}, scan) == []
