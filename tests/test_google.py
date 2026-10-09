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
