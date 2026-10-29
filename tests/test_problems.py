from fastapi.testclient import TestClient

from app import problems, research
from app.main import app
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user


def test_every_problem_has_title_message_and_steps():
    for code in problems._CATALOG:
        p = problems.make(code, "keywords", pages=3, failed=1, count=1, total=4, engines="Copilot", email="a@b.com")
        assert p["title"] and p["message"] and isinstance(p["steps"], list), code
        assert "{" not in p["title"] + p["message"] + " ".join(p["steps"]), code
        assert p["severity"] in {"error", "warning", "info"}


def test_provider_codes_map_to_specific_help():
    m = problems.research_code_for_provider
    assert m(40200) == m(40210) == "provider_balance"
    assert m(40202) == m(40209) == "provider_busy"
    assert m(50401) == "provider_timeout"
    assert m(50301) == "provider_unavailable"
    assert m(40102) == "provider_no_results"
    assert m(40408) == "target_invalid"
    assert m(40505) == "location_required"
    assert m(49999) == "provider_error"


def test_same_site_properties_prefers_owner_and_https():
    sites = [
        {"siteUrl": "http://acme.com/", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://acme.com/", "permissionLevel": "siteOwner"},
        {"siteUrl": "https://other.com/", "permissionLevel": "siteOwner"},
        {"siteUrl": "https://www.acme.com/", "permissionLevel": "siteUnverifiedUser"},
    ]
    got = [s["siteUrl"] for s in problems.same_site_properties(sites, "sc-domain:acme.com")]
    assert got == ["https://acme.com/", "http://acme.com/"]


def test_research_warnings():
    assert problems.research_warnings("keywords", {"ranked": [], "tracked": [], "ideas": []})[0]["code"] == "keywords_empty"
    assert problems.research_warnings("keywords", {"state": "none"}) == []
    vis = problems.research_warnings(
        "ai_visibility",
        {"results": [
            {"status": "ok"},
            {"status": "error", "error": "timeout", "stale": True},
            {"status": "error", "code": "engine_unavailable", "engine": "Copilot"},
        ]},
    )
    assert [w["code"] for w in vis] == ["visibility_partial", "visibility_soon"]
    assert vis[0]["title"] == "1 of 3 AI checks did not finish"
    audit = problems.research_warnings("site_audit", {"state": "ready", "report": {"pagesCrawled": 1, "maxPages": 100}})
    assert audit[0]["code"] == "audit_few_pages" and "1 page" in audit[0]["title"]


def test_research_error_response_keeps_detail_and_adds_problem(monkeypatch):
    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        def boom(*args, **kwargs):
            raise research.ResearchError("Credit empty", code="provider_balance", technical="provider status 40200: x")

        monkeypatch.setattr(research, "backlinks", boom)
        r = TestClient(app).post("/api/v1/research/backlinks", json={"site": "acme.com"})
        body = r.json()
        assert r.status_code == 424
        assert body["detail"] == {"code": "provider_balance", "message": "Credit empty"}
        assert body["problem"]["code"] == "provider_balance" and body["problem"]["area"] == "backlinks"
        assert body["problem"]["technical"].startswith("provider status 40200")
        assert body["problem"]["action"]["do"] == "contact_support"

        monkeypatch.setattr(research, "backlinks", lambda *a, **k: {"host": "acme.com", "links": [], "summary": {"referringDomains": 0}})
        ok = TestClient(app).post("/api/v1/research/backlinks", json={"site": "acme.com"}).json()
        assert ok["ok"] is True and ok["warnings"][0]["code"] == "backlinks_empty"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(user.id)


def test_ai_report_warnings_counts_unestimated_checks():
    report = {"checks": [{"status": "ok"}, {"status": "not_checked", "error": "Visibility model failed: timeout"}]}
    w = problems.ai_report_warnings(report)
    assert w[0]["code"] == "visibility_not_estimated" and w[0]["title"] == "1 of 2 AI checks could not be estimated"
    assert "timeout" in w[0]["technical"]
    assert problems.ai_report_warnings({"checks": [{"status": "ok"}]}) == []


def test_scan_problems():
    assert problems.scan_error_problem("This address is not a public website.")["code"] == "target_invalid"
    p = problems.scan_error_problem("could not load", [{"url": "https://a.com/", "error": "HTTP 403"}])
    assert p["code"] == "scan_site_unreachable" and "HTTP 403" in p["technical"]
    w = problems.scan_warnings({"failedPages": [{"url": "https://a.com/x", "error": "HTTP 404"}]})
    assert w[0]["title"] == "1 page(s) could not be read"
    assert problems.scan_warnings({"failedPages": []}) == []
