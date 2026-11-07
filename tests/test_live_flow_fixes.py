"""Fixes found by running every SEO flow end to end: market from Search Console, rank-check cooldown,
and the HQ system check that explains missing results."""

import pytest

from app import config, markets, problems, site_profiles, system_check
from app.database import SessionLocal
from app.models import FeatureRecord
from tests.db_fixtures import drop_user, make_user


@pytest.fixture
def owner():
    user = make_user()
    yield user
    drop_user(user.id)


def _journey(site):
    return {"planId": "starter", "billing": "monthly", "activeId": 1,
            "sites": [{"id": 1, "status": "ready", "answers": {"site": site}, "createdAt": 1}]}


def _countries(db, user_id, host, rows):
    db.add(FeatureRecord(customer_id=user_id, kind="gsc-countries", title=f"GSC by Country · {host}",
                         payload={"siteHost": host, "rows": rows}, status="stored"))
    db.commit()


def test_market_comes_from_search_console_when_owner_skipped_it(owner):
    with SessionLocal() as db:
        site_profiles.save_journey(db, owner.id, _journey("https://acme.com"))
        assert markets.resolve_for_site(db, owner, site="https://acme.com").get("needsChoice") is True
        _countries(db, owner.id, "acme.com", [["can", "40", "900", "4%", "6"], ["usa", "5", "120", "4%", "9"]])
        ctx = markets.resolve_for_site(db, owner, site="https://acme.com")
    assert ctx["countryIso"] == "CA"
    assert ctx["source"] == "search_console"
    assert not ctx.get("explicit")


def test_split_search_console_traffic_still_asks_for_a_market(owner):
    with SessionLocal() as db:
        site_profiles.save_journey(db, owner.id, _journey("https://acme.com"))
        _countries(db, owner.id, "acme.com", [["can", "4", "300", "1%", "6"], ["usa", "5", "350", "1%", "9"], ["gbr", "1", "250", "1%", "9"]])
        ctx = markets.resolve_for_site(db, owner, site="https://acme.com")
    assert ctx.get("needsChoice") is True


def test_rank_cooldown_is_not_reported_as_a_used_up_daily_limit():
    problem = problems.make("rank_cooldown", "keywords", message="Ranks were checked a moment ago.")
    assert problem["title"] == "Ranks were just checked"
    assert problem["severity"] == "info"
    assert problem["action"] is None


def test_system_check_names_missing_dataforseo_and_openai(monkeypatch):
    monkeypatch.setattr(config, "DATAFORSEO_LOGIN", "")
    monkeypatch.setattr(config, "DATAFORSEO_PASSWORD", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    with SessionLocal() as db:
        report = system_check.run(db)
    by_key = {row["key"]: row for row in report["checks"]}
    assert by_key["dfs"]["state"] == "error" and "DATAFORSEO_LOGIN" in by_key["dfs"]["fix"]
    assert by_key["openai"]["state"] == "error" and "OPENAI_API_KEY" in by_key["openai"]["fix"]
    assert report["errors"] >= 2
