from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import quotas, research, site_profiles, site_scan
from app.database import SessionLocal
from app.main import app
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user


def fake_call(calls):
    def call(method, path, payload=None, timeout=60):
        calls.append(path)
        if path.endswith("/summary/live"):
            return [{"backlinks": 10, "referring_domains": 3}], 0.01
        if path.endswith("/backlinks/live"):
            return [{"items": [], "total_count": 0}], 0.01
        if "ranked_keywords" in path:
            return [{"items": []}], 0.01
        if "keyword_overview" in path:
            return [{"items": [{"keyword": t, "keyword_info": {"search_volume": 10}} for t in payload[0]["keywords"]]}], 0.01
        if "keyword_suggestions" in path:
            return [{"items": [{"keyword": f"idea {i}", "keyword_info": {"search_volume": i}} for i in range(40)]}], 0.01
        raise AssertionError(path)
    return call


@pytest.fixture
def env(monkeypatch):
    user = make_user()
    calls = []
    monkeypatch.setattr(research, "configured", lambda: True)
    monkeypatch.setattr(research, "_call", fake_call(calls))
    app.dependency_overrides[get_current_user] = lambda: user
    with SessionLocal() as db:
        site_profiles.save_journey(db, user.id, {"planId": "starter", "sites": []})
    yield {"client": TestClient(app), "user": user, "calls": calls}
    app.dependency_overrides.pop(get_current_user, None)
    drop_user(user.id)


def test_usage_reports_the_plan_limits(env):
    body = env["client"].get("/api/v1/research/usage").json()
    assert body["plan"] == "starter"
    assert body["trackedKeywords"] == 10
    assert body["features"]["backlinks"] == {"label": "Backlink refreshes", "used": 0, "limit": 5, "left": 5}
    assert set(body["features"]) == {"keywords", "backlinks", "visibility", "competitors", "audits"}


def test_paid_call_counts_and_cached_answer_is_free(env):
    c = env["client"]
    assert c.post("/api/v1/research/backlinks", json={"site": "https://acme.example"}).status_code == 200
    assert c.post("/api/v1/research/backlinks", json={"site": "https://acme.example"}).json()["cached"] is True
    assert c.get("/api/v1/research/usage").json()["features"]["backlinks"]["used"] == 1
    assert len(env["calls"]) == 2


def test_limit_reached_blocks_before_calling_the_provider(env):
    with SessionLocal() as db:
        quotas.consume(db, env["user"], "backlinks", 5)
    r = env["client"].post("/api/v1/research/backlinks", json={"site": "https://acme.example", "force": True})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "quota_exceeded"
    assert env["calls"] == []


def test_starter_tracks_ten_keywords_and_ten_ideas(env):
    terms = [f"term {i}" for i in range(15)]
    body = env["client"].post("/api/v1/research/keywords", json={"site": "https://acme.example", "terms": terms, "country": "Canada"}).json()
    assert len(body["tracked"]) == 10 and body["termsDropped"] == 5
    assert len(body["ideas"]) == 10


def test_admin_has_no_limit():
    admin = SimpleNamespace(id=0, role="ROLE_ADMIN", plan="scale")
    assert quotas.plan_for(None, admin) is None


def test_allowance_resets_each_month():
    assert quotas.period(datetime(2026, 12, 31)) == "2026-12"
    assert quotas.next_reset(datetime(2026, 12, 31)) == datetime(2027, 1, 1)
    assert quotas.next_reset(datetime(2026, 10, 9)) == datetime(2026, 11, 1)


def test_competitor_lookups_stop_at_the_limit(monkeypatch):
    from app import dataforseo

    looked = []
    monkeypatch.setattr(dataforseo, "configured", lambda: True)
    monkeypatch.setattr(dataforseo, "competitor_serp", lambda target, **k: looked.append(target) or [])
    counter = {"serp": 0}
    site_scan.research_competitors(["a", "b", "c", "d"], "acme.example", market="Canada", serp_limit=2, counter=counter)
    assert looked == ["a", "b"] and counter["serp"] == 2
