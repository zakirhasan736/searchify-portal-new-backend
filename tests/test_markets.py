"""Shared target-market configuration: domain defaults, precedence, and tool adapters."""

import pytest
from fastapi.testclient import TestClient

from app import locations, markets, site_profiles
from app.database import SessionLocal
from app.main import app
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user


@pytest.mark.parametrize(
    "host, iso",
    [
        ("https://www.shop.example.ca/path?x=1", "CA"),
        ("blog.acme.co.uk", "GB"),
        ("https://www.brand.com.au/services", "AU"),
        ("store.example.nz", "NZ"),
        ("https://foo.in/x", "IN"),
        ("https://biz.ae", "AE"),
        ("https://site.us/about", "US"),
    ],
)
def test_domain_suggests_country_tlds(host, iso):
    assert markets.suggest_iso_from_host(host) == iso


@pytest.mark.parametrize("host", ["https://acme.com", "https://app.io", "https://org.net", "https://x.org/path"])
def test_generic_tlds_do_not_imply_united_states(host):
    assert markets.suggest_iso_from_host(host) is None


def test_iso_resolve_accepts_selector_codes():
    assert locations.resolve("CA").iso == "CA"
    assert locations.resolve("GB").country == "United Kingdom"


def _state(site_url, market="", site_id=1):
    answers = {"site": site_url}
    if market:
        answers["market"] = market
    return {
        "planId": "starter",
        "billing": "monthly",
        "activeId": site_id,
        "sites": [{"id": site_id, "status": "ready", "answers": answers, "createdAt": site_id}],
    }


@pytest.fixture
def env():
    owner, other = make_user(), make_user()
    current = {"user": owner}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    yield {"client": TestClient(app), "owner": owner, "other": other, "current": current}
    app.dependency_overrides.pop(get_current_user, None)
    drop_user(owner.id)
    drop_user(other.id)


def test_onboarding_market_beats_domain(env):
    with SessionLocal() as db:
        site_profiles.save_journey(db, env["owner"].id, _state("https://acme.ca", market="United States"))
        ctx = markets.resolve_for_site(db, env["owner"], site="https://acme.ca")
    assert ctx["countryIso"] == "US"
    assert ctx["source"] == "onboarding"
    assert ctx["domainSuggestion"] == "CA"


def test_saved_user_market_beats_onboarding_and_domain(env):
    c = env["client"]
    c.put("/api/v1/operator/journey", json={"state": _state("https://acme.ca", market="Canada")})
    r = c.put("/api/v1/operator/market", json={"site": "https://acme.ca", "countryIso": "AU"})
    assert r.status_code == 200
    assert r.json()["market"]["countryIso"] == "AU"
    assert r.json()["market"]["explicit"] is True
    again = c.get("/api/v1/operator/market", params={"site": "https://acme.ca"}).json()["market"]
    assert again["countryIso"] == "AU"
    assert again["source"] == "user"


def test_domain_used_when_no_explicit_market(env):
    with SessionLocal() as db:
        site_profiles.save_journey(db, env["owner"].id, _state("https://widgets.co.uk", market=""))
        ctx = markets.resolve_for_site(db, env["owner"], site="https://widgets.co.uk")
    assert ctx["countryIso"] == "GB"
    assert ctx["source"] == "domain"


def test_com_needs_choice_without_profile(env):
    with SessionLocal() as db:
        site_profiles.save_journey(db, env["owner"].id, _state("https://acme.com", market=""))
        ctx = markets.resolve_for_site(db, env["owner"], site="https://acme.com")
    assert ctx["needsChoice"] is True
    assert not ctx["countryIso"]


def test_get_market_context_adapters(env):
    with SessionLocal() as db:
        site_profiles.save_journey(db, env["owner"].id, _state("https://acme.ca", market="Canada"))
        kw = markets.get_market_context(db, env["owner"], site="https://acme.ca", tool_name="keywords")
        bl = markets.get_market_context(db, env["owner"], site="https://acme.ca", tool_name="backlinks")
        ai = markets.get_market_context(db, env["owner"], site="https://acme.ca", tool_name="visibility")
    assert kw["provider"]["location_name"] == "Canada"
    assert kw["provider"]["language_code"] == "en"
    assert bl["provider"]["countryFilter"] is False
    assert ai["provider"]["iso"] == "CA"


def test_failed_save_keeps_confirmed_market(env):
    c = env["client"]
    c.put("/api/v1/operator/journey", json={"state": _state("https://acme.ca", market="Canada")})
    c.put("/api/v1/operator/market", json={"site": "https://acme.ca", "countryIso": "CA"})
    bad = c.put("/api/v1/operator/market", json={"site": "https://acme.ca", "countryIso": "ZZ"})
    assert bad.status_code == 422
    market = c.get("/api/v1/operator/market", params={"site": "https://acme.ca"}).json()["market"]
    assert market["countryIso"] == "CA"


def test_markets_are_per_tenant(env):
    c = env["client"]
    c.put("/api/v1/operator/journey", json={"state": _state("https://acme.ca", market="Canada")})
    c.put("/api/v1/operator/market", json={"site": "https://acme.ca", "countryIso": "CA"})
    env["current"]["user"] = env["other"]
    assert c.get("/api/v1/operator/market", params={"site": "https://acme.ca"}).json()["market"]["needsChoice"] is True
    bad = c.put("/api/v1/operator/market", json={"site": "https://acme.ca", "countryIso": "US"})
    assert bad.status_code == 422


def test_connected_cms_host_can_save_market_without_journey(env):
    """WordPress/CMS websites are in the workspace even when setup was never finished."""
    from app.database import SessionLocal
    from app.models import CmsConnection

    owner = env["owner"]
    with SessionLocal() as db:
        db.add(CmsConnection(
            customer_id=owner.id,
            provider="wordpress",
            label="sovereignstandard.ca",
            site_url="https://sovereignstandard.ca",
            status="connected",
        ))
        db.commit()
    c = env["client"]
    r = c.put("/api/v1/operator/market", json={"site": "https://sovereignstandard.ca", "countryIso": "CA"})
    assert r.status_code == 200, r.text
    assert r.json()["market"]["countryIso"] == "CA"
    assert r.json()["market"]["host"] == "sovereignstandard.ca"
    got = c.get("/api/v1/operator/market", params={"site": "sovereignstandard.ca"}).json()["market"]
    assert got["countryIso"] == "CA" and got["explicit"] is True
    # .ca also suggests Canada before an explicit save when owned
    with SessionLocal() as db:
        from app import markets
        hinted = markets.resolve_for_site(db, owner, site="https://other.ca")
    # other.ca is not owned
    assert hinted["needsChoice"] is True


def test_cms_label_alone_counts_as_workspace_site(env):
    """Production sometimes stores the host in label when site_url is blank."""
    from app.database import SessionLocal
    from app.models import CmsConnection

    owner = env["owner"]
    with SessionLocal() as db:
        db.add(CmsConnection(
            customer_id=owner.id,
            provider="wordpress",
            label="sovereignstandard.ca",
            site_url="",
            status="connected",
            credentials={"siteUrl": "https://www.sovereignstandard.ca"},
        ))
        db.commit()
    c = env["client"]
    r = c.put("/api/v1/operator/market", json={"site": "https://sovereignstandard.ca", "countryIso": "CA"})
    assert r.status_code == 200, r.text
    assert r.json()["market"]["countryIso"] == "CA"


def test_visibility_retries_without_invalid_country_field(offline_visibility, monkeypatch):
    """Regression: Invalid Field web_search_country_iso_code must not fail the whole check."""
    from app import research

    calls = []

    def fake_call(method, path, payload=None, timeout=60):
        body = dict(payload[0]) if payload else None
        calls.append(body)
        if body and "web_search_country_iso_code" in body:
            raise research.ResearchError("Searchify SEO: Invalid Field: 'web_search_country_iso_code'.")
        return [{"model_name": "m", "items": [{"sections": [{"text": "Acme Plumbing is listed.", "annotations": []}]}]}], 0.0

    monkeypatch.setattr(research, "_call", fake_call)
    out = research.visibility(
        None,
        type("U", (), {"id": 1, "role": "ROLE_USER", "plan": "starter"})(),
        site="https://acme.ca",
        brand="Acme",
        country="Canada",
        prompts=[{"id": 1, "text": "best plumber", "engine": "ChatGPT"}],
        force=True,
    )
    assert out["results"][0]["status"] == "ok"
    assert len(calls) == 2
    assert "web_search_country_iso_code" in calls[0]
    assert "web_search_country_iso_code" not in calls[1]


@pytest.fixture
def offline_visibility(monkeypatch):
    from app import research

    saved = {}
    monkeypatch.setattr(research, "cached", lambda *a, **k: None)
    monkeypatch.setattr(research, "_record", lambda db, uid, kind, title: type("R", (), {"payload": saved.get(title)})() if title in saved else None)
    monkeypatch.setattr(research, "_save", lambda db, uid, kind, title, payload: saved.__setitem__(title, payload))
    monkeypatch.setattr(research, "_budget", lambda *a, **k: None)
    monkeypatch.setattr(research, "_spend", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "plan_for", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "left", lambda *a, **k: None)
    monkeypatch.setattr(research, "_model_for", lambda *a, **k: {"name": "gpt-4o-mini", "reasoning": False, "webSearch": True})
    return saved
