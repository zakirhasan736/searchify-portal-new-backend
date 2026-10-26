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


# --- Property detection by the site's domain -------------------------------------------------------------

def _row(user_id):
    with SessionLocal() as db:
        return db.query(GoogleConnection).filter(GoogleConnection.customer_id == user_id).first()


def test_missed_match_is_retried_later(env, monkeypatch):
    """A site whose property did not exist yet is matched once it appears, not stuck empty forever."""
    c, uid = env["client"], env["user"].id
    monkeypatch.setattr(goauth, "list_gsc_sites", lambda token: [])
    monkeypatch.setattr(goauth, "list_ga4_properties", lambda token: [])
    first = c.post("/api/v1/oauth/google/activate-site", json={"site": "https://acme.com"}).json()
    assert first["matched"] is False
    monkeypatch.setattr(goauth, "list_gsc_sites", lambda token: SITES)
    monkeypatch.setattr(goauth, "list_ga4_properties", lambda token: [{"propertyId": "1", "displayName": "Acme", "accountName": "A"}])
    # Within the retry window nothing is asked of Google again.
    assert c.post("/api/v1/oauth/google/activate-site", json={"site": "https://acme.com"}).json()["matched"] is False
    with SessionLocal() as db:  # the retry window has passed
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == uid).first()
        meta = dict(row.meta)
        meta["byHost"]["acme.com"]["checkedAt"] = (datetime.utcnow() - timedelta(minutes=11)).isoformat()
        row.meta = meta
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "meta")
        db.commit()
    again = c.post("/api/v1/oauth/google/activate-site", json={"site": "https://acme.com"}).json()
    assert again["matched"] is True
    assert again["connection"]["gsc"]["status"] == "connected"
    assert again["connection"]["gscSiteUrl"] == "sc-domain:acme.com"


def test_owner_choice_is_never_overwritten(env):
    c = env["client"]
    assert c.post("/api/v1/oauth/google/select", json={"gsc_site_url": "https://www.acme.com/", "site": "acme.com"}).status_code == 200
    r = c.post("/api/v1/oauth/google/activate-site", json={"site": "acme.com"}).json()
    assert r["connection"]["gscSiteUrl"] == "https://www.acme.com/"  # not replaced by the sc-domain match
    assert r["connection"]["ga4PropertyId"] == "1"  # the missing GA4 part was filled by the match


def test_ga4_only_choice_is_kept_for_its_site(env):
    c = env["client"]
    assert c.post("/api/v1/oauth/google/select", json={"ga4_property_id": "1", "site": "https://shop.example/"}).status_code == 200
    conn = c.get("/api/v1/oauth/google/status", params={"site": "shop.example"}).json()["connection"]
    assert conn["ga4"]["status"] == "connected" and conn["ga4PropertyId"] == "1"


def test_connect_during_setup_matches_the_draft_site(env):
    from app import site_profiles
    from app.routers.google_connect import _auto_match_after_connect

    uid = env["user"].id
    with SessionLocal() as db:
        site_profiles.save_journey(db, uid, {"sites": [{"id": "s1", "status": "draft", "answers": {"site": "https://www.acme.com"}}], "activeId": "s1"})
        db.commit()
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == uid).first()
        _auto_match_after_connect(db, row)
    conn = env["client"].get("/api/v1/oauth/google/status", params={"site": "acme.com"}).json()["connection"]
    assert conn["gsc"]["status"] == "connected" and conn["gscSiteUrl"] == "sc-domain:acme.com"
    assert conn["ga4"]["status"] == "connected" and conn["ga4PropertyId"] == "1"


def test_google_error_does_not_break_opening_a_site(env, monkeypatch):
    def down(token):
        raise RuntimeError("GSC sites failed: 503")

    monkeypatch.setattr(goauth, "list_gsc_sites", down)
    r = env["client"].post("/api/v1/oauth/google/activate-site", json={"site": "acme.com"})
    assert r.status_code == 200 and r.json()["matched"] is False


def test_google_sign_in_returns_to_where_it_started(env, monkeypatch):
    monkeypatch.setattr(goauth, "google_configured", lambda: True)
    monkeypatch.setattr("app.routers.google_connect._ensure_google_config", lambda: None)
    import urllib.parse as up

    url = env["client"].get("/api/v1/oauth/google/start", params={"services": "gsc,ga4", "next": "/app/start", "add": "1"}).json()["authUrl"]
    state = up.parse_qs(up.urlparse(url).query)["state"][0]
    assert goauth.parse_oauth_state(state)["next"] == "/app/start"
    url = env["client"].get("/api/v1/oauth/google/start", params={"services": "gsc,ga4", "next": "https://evil.example/", "add": "1"}).json()["authUrl"]
    state = up.parse_qs(up.urlparse(url).query)["state"][0]
    assert goauth.parse_oauth_state(state)["next"] == ""
