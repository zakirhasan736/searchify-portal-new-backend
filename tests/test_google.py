from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import google_match as gm
from app import google_oauth as goauth
from app.database import SessionLocal
from app.main import app
from app.models import GoogleConnection
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user

SITES = [
    {"siteUrl": "sc-domain:acme.com", "permissionLevel": "siteOwner"},
    {"siteUrl": "https://www.acme.com/", "permissionLevel": "siteFullUser"},
    {"siteUrl": "https://other.com/", "permissionLevel": "siteOwner"},
]


def test_domain_property_is_the_unambiguous_match():
    assert gm.match_gsc("https://acme.com", SITES) == {"status": "matched", "selected": "sc-domain:acme.com",
                                                      "candidates": ["sc-domain:acme.com", "https://www.acme.com/"]}


def test_www_and_scheme_variants():
    props = [{"siteUrl": "https://www.acme.com/"}, {"siteUrl": "http://acme.com/"}]
    assert gm.match_gsc("acme.com", props)["selected"] == "https://www.acme.com/"
    assert gm.match_gsc("http://acme.com", props)["selected"] == "http://acme.com/"


def test_two_equal_properties_are_ambiguous():
    props = [{"siteUrl": "https://acme.com/"}, {"siteUrl": "https://www.acme.com/"}]
    found = gm.match_gsc("http://shop.example", props)
    assert found["status"] == "none"
    found = gm.match_gsc("https://acme.com", [{"siteUrl": "http://acme.com/"}, {"siteUrl": "http://www.acme.com/"}])
    assert found["status"] == "ambiguous" and len(found["candidates"]) == 2


def test_unverified_property_is_never_matched():
    assert gm.match_gsc("acme.com", [{"siteUrl": "sc-domain:acme.com", "permissionLevel": "siteUnverifiedUser"}])["status"] == "none"


def test_ga4_matches_by_web_stream():
    props = [{"propertyId": "1", "displayName": "Acme", "streams": ["https://www.acme.com"]},
             {"propertyId": "2", "displayName": "Other", "streams": ["https://other.com"]}]
    assert gm.match_ga4("acme.com", props)["selected"] == "1"
    props.append({"propertyId": "3", "displayName": "Acme old", "streams": ["http://acme.com/"]})
    assert gm.match_ga4("acme.com", props)["status"] == "ambiguous"
    assert gm.match_ga4("nothing.com", props)["status"] == "none"


@pytest.fixture
def env(monkeypatch):
    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    with SessionLocal() as db:
        db.add(GoogleConnection(customer_id=user.id, google_email="owner@acme.com", access_token="a", refresh_token="r",
                                token_expiry=datetime.utcnow() + timedelta(hours=1), status="connected", scopes=[], meta={}))
        db.commit()
    monkeypatch.setattr(goauth, "list_gsc_sites", lambda token: SITES)
    monkeypatch.setattr(goauth, "list_ga4_properties", lambda token: [{"propertyId": "1", "displayName": "Acme", "accountName": "A"}])
    monkeypatch.setattr(goauth, "list_ga4_streams", lambda token, pid: ["https://acme.com"] if pid == "1" else [])
    # Never call Google from tests: the access probe succeeds for properties in SITES.
    monkeypatch.setattr(goauth, "probe_gsc_access", lambda token, site_url, email="": {"ok": True, "siteUrl": site_url})
    yield {"client": TestClient(app), "user": user}
    app.dependency_overrides.pop(get_current_user, None)
    drop_user(user.id)


def test_account_and_properties_have_separate_statuses(env):
    c = env["client"]
    conn = c.get("/api/v1/oauth/google/status").json()["connection"]
    assert conn["account"] == {"status": "connected", "email": "owner@acme.com"}
    assert conn["gsc"]["status"] == "not_selected" and conn["ga4"]["status"] == "not_selected"


def test_sites_endpoint_returns_match(env):
    data = env["client"].get("/api/v1/oauth/google/sites", params={"site": "https://acme.com"}).json()
    assert data["match"]["gsc"]["selected"] == "sc-domain:acme.com"
    assert data["match"]["ga4"]["selected"] == "1"


def test_select_validates_access(env):
    c = env["client"]
    r = c.post("/api/v1/oauth/google/select", json={"gsc_site_url": "https://not-mine.com/"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "gsc_no_access"
    r = c.post("/api/v1/oauth/google/select", json={"ga4_property_id": "999"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ga4_no_access"
    r = c.post("/api/v1/oauth/google/select", json={"gsc_site_url": "sc-domain:acme.com", "ga4_property_id": "1"})
    assert r.status_code == 200
    assert r.json()["gsc"]["status"] == "connected" and r.json()["ga4"]["status"] == "connected"
    assert r.json()["ga4"]["propertyName"] == "Acme"


def test_revoked_refresh_token_needs_reconnect(env, monkeypatch):
    with SessionLocal() as db:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        row.token_expiry = datetime.utcnow() - timedelta(hours=1)
        db.commit()

    def revoked(token):
        raise goauth.GoogleReauthRequired("invalid_grant")

    monkeypatch.setattr(goauth, "refresh_access_token", revoked)
    r = env["client"].get("/api/v1/oauth/google/sites")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "google_reauth_required"
    conn = env["client"].get("/api/v1/oauth/google/status").json()["connection"]
    assert conn["account"]["status"] == "reauth_required" and conn["connected"] is False
    assert conn["gsc"]["status"] == "not_selected"
    with SessionLocal() as db:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        assert row.access_token == "" and "Reconnect Google" in row.last_error


def test_status_never_exposes_tokens(env):
    text = env["client"].get("/api/v1/oauth/google/status").text
    assert '"a"' not in text and '"r"' not in text and "accessToken" not in text and "refreshToken" not in text


def test_no_connection_uses_the_same_status_shape():
    from app.routers.google_connect import _connection_payload

    payload = _connection_payload(None)
    assert payload["connected"] is False
    assert payload["account"] == {"status": "disconnected", "email": ""}
    assert payload["gsc"]["status"] == "not_selected" and payload["ga4"]["status"] == "not_selected"


def test_select_overwrites_stale_property_on_the_website(env):
    """Regression: website meta kept sc-domain:… and sync used it instead of the saved https:// choice."""
    from app.models import CmsConnection
    from app.routers.google_connect import _apply_site_google

    with SessionLocal() as db:
        cms = CmsConnection(customer_id=env["user"].id, provider="wordpress", site_url="https://acme.com",
                            status="connected", credentials={}, meta={"gscSiteUrl": "sc-domain:acme.com"})
        db.add(cms)
        db.commit()
        cms_id = cms.id
    # Saved from the Connect screen: no website id is sent.
    r = env["client"].post("/api/v1/oauth/google/select", json={"gsc_site_url": "https://www.acme.com/"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(CmsConnection, cms_id).meta["gscSiteUrl"] == "https://www.acme.com/"
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        _apply_site_google(db, env["user"].id, row, None)
        assert row.gsc_site_url == "https://www.acme.com/"


def test_select_denied_returns_problem_with_help(env):
    r = env["client"].post("/api/v1/oauth/google/select", json={"gsc_site_url": "sc-domain:not-mine.com"})
    body = r.json()
    assert r.status_code == 422
    assert body["detail"]["code"] == "gsc_no_access"  # old shape kept
    problem = body["problem"]
    assert problem["code"] == "gsc_property_no_access" and problem["area"] == "search_console"
    assert problem["steps"] and problem["action"]["href"].startswith("https://search.google.com/search-console/users")


def test_sync_403_suggests_the_property_the_account_can_read(env):
    from app.routers.google_connect import _google_problem

    with SessionLocal() as db:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        row.gsc_site_url = "sc-domain:acme.com"
        sites = [
            {"siteUrl": "sc-domain:acme.com", "permissionLevel": "siteUnverifiedUser"},
            {"siteUrl": "https://acme.com/", "permissionLevel": "siteOwner"},
        ]
        goauth_list = goauth.list_gsc_sites
        goauth.list_gsc_sites = lambda token: sites
        try:
            exc = goauth.GoogleApiError("denied", service="gsc", status=403, resource="sc-domain:acme.com", raw="403 PERMISSION_DENIED")
            problem = _google_problem(exc, row, "token")
        finally:
            goauth.list_gsc_sites = goauth_list
    assert problem["code"] == "gsc_property_no_access"
    assert problem["action"] == {"label": "Use https://acme.com/", "do": "open_property_picker", "value": "https://acme.com/"}
    assert "Owner" in problem["message"] and problem["alternatives"][0]["siteUrl"] == "https://acme.com/"
    assert "403 PERMISSION_DENIED" in problem["technical"]


def test_ga4_403_is_an_analytics_problem(env):
    from app.routers.google_connect import _google_problem

    with SessionLocal() as db:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        exc = goauth.GoogleApiError("GA4 report failed", service="ga4", status=403, resource="123")
        problem = _google_problem(exc, row, "")
    assert problem["code"] == "ga4_no_access" and problem["area"] == "analytics"
    assert "owner@acme.com" in " ".join(problem["steps"])


def test_sync_denied_returns_problem_and_status_keeps_it(env, monkeypatch):
    with SessionLocal() as db:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        row.gsc_site_url = "https://acme.com/"
        db.commit()

    def denied(token, site_url, **kwargs):
        raise goauth.GoogleApiError("no", service="gsc", status=403, resource=site_url, raw="User does not have sufficient permission")

    monkeypatch.setattr(goauth, "fetch_gsc_top_queries", denied)
    r = env["client"].post("/api/v1/oauth/google/sync", json={})
    body = r.json()
    assert r.status_code == 502 and isinstance(body["detail"], str)
    assert body["problem"]["code"] == "gsc_property_no_access"
    assert body["problem"]["action"]["value"] == "sc-domain:acme.com"  # Owner beats Full
    conn = env["client"].get("/api/v1/oauth/google/status").json()["connection"]
    assert conn["problem"]["code"] == "gsc_property_no_access" and conn["lastError"]


def test_inspect_endpoint_reports_google_issues_and_stores_feature(env, monkeypatch):
    with SessionLocal() as db:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == env["user"].id).first()
        row.gsc_site_url = "sc-domain:acme.com"
        db.commit()

    def fake_inspect(token, site_url, url, language="en-US"):
        if url.endswith("/old"):
            return {"inspectionResult": {"indexStatusResult": {"verdict": "FAIL", "coverageState": "Not found (404)", "pageFetchState": "NOT_FOUND"}}}
        return {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}}

    monkeypatch.setattr(goauth, "inspect_url", fake_inspect)
    r = env["client"].post("/api/v1/oauth/google/inspect", json={"urls": ["https://acme.com/", "https://acme.com/old"]})
    body = r.json()
    assert r.status_code == 200, body
    assert body["inspected"] == 2 and body["indexed"] == 1
    assert body["issues"][0]["severity"] == "Critical" and body["issues"][0]["area"] == "Search Console"
    stored = env["client"].get("/api/v1/features/gsc-index-inspection").json()
    assert stored  # feature saved for the audit page


def test_inspect_requires_property(env):
    r = env["client"].post("/api/v1/oauth/google/inspect", json={})
    assert r.status_code == 422 and r.json()["problem"]["code"] == "gsc_not_selected"
