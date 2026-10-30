from app import ai_readiness as ar
from app.audit_checks import scan_issues


def _bot(rows, agent):
    return next(r for r in rows if r["agent"] == agent)


def test_robots_specific_group_beats_star_and_longest_rule_wins():
    text = """
User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php

User-agent: GPTBot
Disallow: /

User-agent: ClaudeBot
User-agent: CCBot
Disallow: /

User-agent: PerplexityBot
Disallow: /
Allow: /
"""
    rows = ar.robots_ai_access(text, 200, ["/", "/services/"])
    assert not _bot(rows, "GPTBot")["allowed"]
    assert _bot(rows, "OAI-SearchBot")["allowed"]  # falls back to *, which allows /
    assert not _bot(rows, "ClaudeBot")["allowed"] and not _bot(rows, "CCBot")["allowed"]
    assert _bot(rows, "PerplexityBot")["allowed"]  # Allow wins a tie
    assert _bot(rows, "GPTBot")["group"] == "GPTBot"
    assert _bot(rows, "OAI-SearchBot")["group"] == "*"


def test_wildcards_and_end_anchor():
    assert ar.allowed([("disallow", "/*.pdf$")], "/a/file.pdf") == (False, "Disallow: /*.pdf$")
    assert ar.allowed([("disallow", "/*.pdf$")], "/a/file.pdf?x=1")[0] is True
    assert ar.allowed([("disallow", "")], "/")[0] is True


def test_robots_5xx_blocks_everyone():
    rows = ar.robots_ai_access("", 503)
    assert all(not r["allowed"] for r in rows)
    reasons = ar.crawler_reasons({"robots": {"status": 503, "bots": rows}})
    assert [r["key"] for r in reasons] == ["ai_robots_5xx"]


def test_crawler_reasons_split_answer_training_and_google():
    text = "User-agent: OAI-SearchBot\nDisallow: /\n\nUser-agent: GPTBot\nDisallow: /\n\nUser-agent: Googlebot\nDisallow: /\n"
    checks = {"robots": {"status": 200, "bots": ar.robots_ai_access(text, 200)}}
    by_key = {r["key"]: r for r in ar.crawler_reasons(checks)}
    assert by_key["ai_answer_bots_blocked"]["severity"] == "Critical"
    assert "ChatGPT" in by_key["ai_answer_bots_blocked"]["engines"]
    assert by_key["ai_training_bots_blocked"]["severity"] == "Notice"
    assert "Google AI Overviews" in by_key["ai_search_bots_blocked"]["engines"]
    assert all(r["area"] == "AI search" and r["steps"] for r in by_key.values())


def test_firewall_probe_reason():
    checks = {"robots": {"status": 200, "bots": ar.robots_ai_access("", 200)},
              "probe": [{"agent": "GPTBot", "status": 403, "challenge": False}, {"agent": "ClaudeBot", "status": 200, "challenge": True},
                        {"agent": "PerplexityBot", "status": 200, "challenge": False}]}
    reasons = ar.crawler_reasons(checks)
    assert [r["key"] for r in reasons] == ["ai_firewall_blocks"]
    assert reasons[0]["count"] == 2


def test_llms_txt_states():
    assert ar.check_llms_txt(404, "")["problem"] == "missing"
    assert ar.check_llms_txt(200, "<!DOCTYPE html><html>", "text/html")["problem"] == "html"
    assert ar.check_llms_txt(200, "hello", "text/plain")["problem"] == "format"
    ok = ar.check_llms_txt(200, "# Acme\n> Plumbing\n- [Services](https://acme.ca/s): what we do\n", "text/plain")
    assert ok["problem"] == "" and ok["links"] == 1 and ok["title"] == "Acme"


def test_directives_and_page_signals():
    d = ar.robots_directives("index, max-snippet:0", "noai")
    assert "max-snippet:0" in d["directives"] and "noai" in d["directives"]
    assert ar.robots_directives("none", "")["directives"] == ["noindex", "none", "nosnippet"]
    assert ar.robots_directives("noindexed-thing", "")["directives"] == []
    keys = {r["key"] for r in ar.page_signal_reasons({"home": {"status": 200, "url": "https://a.ca/", **ar.robots_directives("noindex,nosnippet", "")},
                                                      "llms": {"problem": "missing", "status": 404}})}
    assert {"ai_home_noindex", "ai_nosnippet", "ai_llms_txt_missing"} <= keys


def test_meta_robots_parser():
    html = '<head><meta name="robots" content="index, nosnippet"><meta content="noindex" name="googlebot"></head>'
    assert ar._meta_robots(html) == "index, nosnippet,noindex"


def _scan():
    return {"pages": [
        {"url": "https://acme.ca/", "title": "Home", "h1": "Welcome", "text": "We fix pipes.", "words": 80, "schemaTypes": ["WebSite"], "faqs": []},
        {"url": "https://acme.ca/a", "title": "A", "text": "x", "words": 90, "noindex": True, "faqs": []},
        {"url": "https://acme.ca/b", "title": "B", "text": "x", "words": 600, "faqs": [],
         "audit": {"schema": {"types": ["Service"]}, "questions": []}},
    ]}


def test_content_and_indexing_reasons():
    keys = {r["key"] for r in ar.content_reasons(_scan(), {"business": "Acme Plumbing"})}
    assert {"ai_no_entity_schema", "ai_no_questions", "ai_brand_missing_home", "ai_thin_pages"} <= keys
    scan = _scan()
    scan["pages"][0]["schemaTypes"] = ["Plumber"]
    scan["pages"][0]["text"] = "Acme  Plumbing fixes pipes"
    keys = {r["key"] for r in ar.content_reasons(scan, {"business": "Acme Plumbing"})}
    assert "ai_no_entity_schema" not in keys and "ai_brand_missing_home" not in keys
    idx = ar.indexing_reasons(_scan(), {"rows": [["https://acme.ca/", "Submitted and indexed", "PASS"], ["https://acme.ca/b", "Crawled - currently not indexed", "NEUTRAL"]]})
    by_key = {r["key"]: r for r in idx}
    assert by_key["ai_pages_noindex"]["count"] == 1
    assert by_key["ai_gsc_not_indexed"]["count"] == 1


def test_llm_reasons_cover_model_errors():
    assert ar.llm_reasons({"summary": "OpenAI is not configured, so …", "checks": [{}]})[0]["key"] == "ai_llm_not_configured"
    for text, key in [("Visibility model failed: RateLimitError 429", "ai_llm_rate_limited"),
                      ("Visibility model failed: insufficient_quota", "ai_llm_quota"),
                      ("Visibility model failed: AuthenticationError 401 Incorrect API key", "ai_llm_key_invalid"),
                      ("Visibility model failed: ReadTimeout", "ai_llm_timeout"),
                      ("Visibility model failed: boom", "ai_llm_failed")]:
        r = ar.llm_reasons({"summary": text, "checks": [{"status": "not_checked"}]})[0]
        assert r["key"] == key and r["group"] == "llm" and r["technical"].startswith("Visibility")
    partial = ar.llm_reasons({"summary": "ok", "checks": [{"status": "ok", "engine": "ChatGPT"}, {"status": "not_checked", "engine": "Grok"}]})
    assert partial[0]["key"] == "ai_llm_partial" and partial[0]["engines"] == ["Grok"]
    assert ar.llm_reasons({"status": "unavailable"})[0]["key"] == "ai_no_report"
    assert any(r["key"] == "ai_gsc_not_synced" for r in ar.llm_reasons({"summary": "ok", "checks": []}, gsc_synced=False))


def test_visibility_reasons_sorted_and_deduped():
    checks = {"robots": {"status": 200, "bots": ar.robots_ai_access("User-agent: OAI-SearchBot\nDisallow: /\n", 200)}, "llms": {"problem": "missing"}}
    reasons = ar.visibility_reasons(report={"summary": "ok", "checks": []}, checks=checks, scan=_scan(), inspection={}, profile={})
    sev = [r["severity"] for r in reasons]
    assert sev == sorted(sev, key=lambda s: ar.SEVERITY_ORDER[s])
    assert len({r["key"] for r in reasons}) == len(reasons)
    assert reasons[0]["key"] == "ai_answer_bots_blocked"


def test_scan_issues_include_ai_search_topic():
    scan = _scan()
    scan["aiChecks"] = {"robots": {"status": 200, "bots": ar.robots_ai_access("User-agent: GPTBot\nDisallow: /\n", 200)}, "llms": {"problem": "missing"}, "home": {}}
    ai = [i for i in scan_issues(scan) if i["area"] == "AI search"]
    keys = {i["key"] for i in ai}
    assert {"ai_training_bots_blocked", "ai_llms_txt_missing", "ai_no_entity_schema"} <= keys
    assert "ai_pages_noindex" not in keys  # the audit already has a noindex finding


def test_crawler_table():
    rows = ar.crawler_table({"robots": {"bots": ar.robots_ai_access("User-agent: GPTBot\nDisallow: /\n", 200)}})
    gpt = next(r for r in rows if r["agent"] == "GPTBot")
    assert gpt == {"agent": "GPTBot", "company": "OpenAI", "engines": ["ChatGPT"], "role": "training", "allowed": False, "rule": "Disallow: /"}


def test_ai_visibility_endpoint_returns_reasons(monkeypatch):
    from fastapi.testclient import TestClient

    from app.security import get_current_user
    from app.database import SessionLocal
    from app.main import app
    from app.models import CmsConnection
    from tests.db_fixtures import drop_user, make_user

    user = make_user()
    calls = []

    def fake_checks(home, sample=None, timeout=6.0):
        calls.append(home)
        return {"checkedAt": "2099-01-01T00:00:00", "host": "acme.example",
                "robots": {"status": 200, "bots": ar.robots_ai_access("User-agent: OAI-SearchBot\nDisallow: /\n", 200)},
                "llms": {"status": 404, "present": False, "problem": "missing"}, "home": {"status": 200, "directives": []}, "probe": []}

    monkeypatch.setattr(ar, "run_ai_checks", fake_checks)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with SessionLocal() as db:
            db.add(CmsConnection(customer_id=user.id, provider="wordpress", site_url="https://acme.example", credentials={}, status="connected"))
            db.commit()
        c = TestClient(app)
        body = c.get("/api/v1/operator/ai-visibility").json()
        ready = body["readiness"]
        keys = [r["key"] for r in ready["reasons"]]
        assert keys[0] == "ai_answer_bots_blocked"
        assert "ai_llms_txt_missing" in keys and "ai_no_report" in keys
        assert any(r["agent"] == "OAI-SearchBot" and not r["allowed"] for r in ready["crawlers"])
        assert ready["host"] == "acme.example"
        # Stored: a second GET doesn't hit the network again; the re-check endpoint does.
        c.get("/api/v1/operator/ai-visibility")
        assert len(calls) == 1
        assert c.post("/api/v1/operator/ai-visibility/readiness").status_code == 200
        assert len(calls) == 2
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(user.id)
