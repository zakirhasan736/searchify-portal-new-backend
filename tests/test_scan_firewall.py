"""Scans must work on real WordPress hosts: firewalls that refuse bot names, robots.txt, the active site."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import site_profiles, site_scan
from app.database import SessionLocal
from app.main import app
from app.security import get_current_user
from tests import fake_site
from tests.db_fixtures import drop_user, make_user

HOME = f"https://{fake_site.HOST}/"


@pytest.fixture
def web(monkeypatch):
    """Route every httpx call to the fake site (the test client itself keeps its own transport)."""
    real_init = httpx.Client.__init__

    def init(self, *args, **kwargs):
        if type(self).__name__ != "TestClient":
            kwargs["transport"] = fake_site.TRANSPORT
        real_init(self, *args, **kwargs)

    def get(url, **kwargs):
        follow = kwargs.pop("follow_redirects", False)
        with httpx.Client(headers=kwargs.pop("headers", None), timeout=kwargs.pop("timeout", 10)) as client:
            return client.get(url, follow_redirects=follow)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(site_scan, "public_host", lambda url: True)
    monkeypatch.setitem(fake_site.MODE, "waf", False)
    site_scan._browser_hosts.clear()
    fake_site.LOG.clear()
    yield fake_site
    site_scan._browser_hosts.clear()


def test_robots_and_sitemap_are_read(web):
    status, text, _ = site_scan.fetch_robots(HOME)
    assert status == 200 and "Sitemap:" in text
    crawled = site_scan.crawl(HOME, limit=10)
    assert len(crawled["pages"]) == 4 and not crawled["readAsBrowser"]


def test_firewall_that_refuses_the_bot_name_still_gets_read(web):
    web.MODE["waf"] = True
    crawled = site_scan.crawl(HOME, limit=10)
    assert len(crawled["pages"]) == 4, crawled["failed"]
    assert crawled["readAsBrowser"] is True and crawled["error"] == ""


def test_site_that_refuses_everyone_explains_why(web, monkeypatch):
    monkeypatch.setattr(site_scan, "polite_get", lambda client, url, **kw: httpx.Response(403, request=httpx.Request("GET", url)))
    crawled = site_scan.crawl(HOME, limit=10)
    assert crawled["pages"] == [] and "firewall" in crawled["error"].lower()


def test_unresolvable_domain_says_so():
    assert "could not find" in site_scan.host_problem("https://no-such-host.invalid/")


def test_scan_without_a_site_uses_the_active_site_and_health_reports_it(web):
    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with SessionLocal() as db:
            site_profiles.save_journey(db, user.id, {"sites": [{"id": "s1", "status": "ready", "answers": {"site": HOME, "market": "Calgary, Alberta"}}], "activeId": "s1"})
            db.commit()
        with TestClient(app) as c:
            before = c.get("/api/v1/operator/data-health").json()
            assert any(x["key"] == "scan" and x["state"] == "error" for x in before["checks"])
            r = c.post("/api/v1/operator/site-scan", json={})  # no site: the active onboarding site is scanned
            assert r.status_code == 200, r.text
            assert r.json()["scan"]["pagesRead"] == 4
            after = c.get("/api/v1/operator/data-health").json()
            keys = {x["key"]: x["state"] for x in after["checks"]}
            assert keys["scan"] == "ok" and keys["robots"] == "ok"
            assert keys["research"] == "error"  # no research provider in tests: the reason is shown, not hidden
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(user.id)
