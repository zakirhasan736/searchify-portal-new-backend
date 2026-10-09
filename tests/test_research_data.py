import pytest

from app import research
from app.locations import LocationError


class FakeUser:
    id = 1
    role = "ROLE_USER"
    plan = "starter"


@pytest.fixture
def offline(monkeypatch):
    calls = []
    monkeypatch.setattr(research, "cached", lambda *a, **k: None)
    monkeypatch.setattr(research, "_save", lambda *a, **k: None)
    monkeypatch.setattr(research, "_budget", lambda *a, **k: None)
    monkeypatch.setattr(research, "_spend", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "plan_for", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)
    return calls


def ranked_item(keyword, rank, url, volume=None):
    return {"keyword_data": {"keyword": keyword, "keyword_info": {"search_volume": volume}},
            "ranked_serp_element": {"serp_item": {"rank_group": rank, "url": url, "relative_url": url[17:]}}}


def test_keywords_keep_location_language_nulls_and_dedupe(offline, monkeypatch):
    def fake_call(method, path, payload=None, timeout=60):
        offline.append((path, payload[0]))
        if "ranked_keywords" in path:
            return [{"items": [
                ranked_item("calgary plumber", 9, "https://acme.ca/blog/x", 500),
                ranked_item("calgary plumber", 3, "https://acme.ca/plumbing", 500),
                ranked_item("drain repair", 12, "https://acme.ca/drains"),
            ]}], 0.01
        if "keyword_overview" in path:
            return [{"items": [{"keyword": "calgary plumber", "keyword_info": {"search_volume": 500}}]}], 0.01
        return [{"items": [{"keyword": "emergency plumber calgary", "keyword_info": {"search_volume": None}}]}], 0.01

    monkeypatch.setattr(research, "_call", fake_call)
    out = research.keywords(None, FakeUser(), site="acme.ca", terms=["Calgary Plumber", "calgary plumber", "unknown term"], country="Calgary, Alberta")
    assert out["location"] == "Canada" and out["language"] == "en" and out["place"]["city"] == "Calgary"
    assert all(p["location_name"] == "Canada" and p["language_code"] == "en" for _, p in offline)
    ranked = {r["keyword"]: r for r in out["ranked"]}
    assert len(out["ranked"]) == 2
    assert ranked["calgary plumber"]["position"] == 3 and ranked["calgary plumber"]["url"].endswith("/plumbing")
    assert ranked["calgary plumber"]["otherUrls"] == ["https://acme.ca/blog/x"]
    assert ranked["drain repair"]["volume"] is None
    tracked = {r["keyword"]: r for r in out["tracked"]}
    assert set(tracked) == {"calgary plumber", "unknown term"}
    assert tracked["unknown term"]["volume"] is None and tracked["unknown term"]["position"] is None
    assert tracked["unknown term"]["source"] == "Not in DataForSEO keyword data"
    assert out["ideas"][0]["volume"] is None and out["ideas"][0]["source"] == "DataForSEO keyword suggestions"


def test_keywords_refuse_to_guess_location(offline, monkeypatch):
    monkeypatch.setattr(research, "_call", lambda *a, **k: pytest.fail("must not call DataForSEO"))
    with pytest.raises(LocationError):
        research.keywords(None, FakeUser(), site="acme.ca", terms=["x"], country="")


def test_backlinks_include_lost_links_dedupe_and_keep_nulls(offline, monkeypatch):
    sent = {}

    def fake_call(method, path, payload=None, timeout=60):
        sent[path] = payload[0]
        if "summary" in path:
            return [{"backlinks": 120, "referring_domains": None, "rank": 41}], 0.02
        return [{"total_count": 3, "items": [
            {"domain_from": "www.blog.com", "url_from": "https://blog.com/a", "url_to": "https://acme.ca/", "is_lost": True, "date_lost": "2026-08-01"},
            {"domain_from": "blog.com", "url_from": "https://blog.com/b", "url_to": "https://acme.ca/"},
            {"domain_from": "news.com", "url_from": "https://news.com/x", "url_to": "https://acme.ca/p", "is_new": True, "dofollow": True},
        ]}], 0.02

    monkeypatch.setattr(research, "_call", fake_call)
    out = research.backlinks(None, FakeUser(), site="https://www.acme.ca/")
    assert sent["/backlinks/backlinks/live"]["backlinks_status_type"] == "all"
    assert sent["/backlinks/summary/live"]["target"] == "acme.ca"
    assert out["summary"]["backlinks"] == 120 and out["summary"]["referringDomains"] is None
    assert [l["domain"] for l in out["links"]] == ["www.blog.com", "news.com"]
    assert out["links"][0]["state"] == "Lost" and out["links"][0]["lostDate"] == "2026-08-01"
    assert out["links"][1]["state"] == "New"
    assert out["referringDomainsListed"] == 3 and out["linksShown"] == 2


def test_backlinks_stored_only_never_calls_dataforseo(offline, monkeypatch):
    monkeypatch.setattr(research, "_call", lambda *a, **k: pytest.fail("stored_only must not call DataForSEO"))
    monkeypatch.setattr(research, "_record", lambda *a, **k: None)
    out = research.backlinks(None, FakeUser(), site="https://example.com", stored_only=True)
    assert out == {"host": "example.com", "state": "none", "cached": True}

    class Row:
        payload = {"host": "example.com", "summary": {"referringDomains": 42}, "fetchedAt": "2026-10-01T00:00:00"}

    monkeypatch.setattr(research, "_record", lambda *a, **k: Row())
    out = research.backlinks(None, FakeUser(), site="https://example.com", stored_only=True)
    assert out["summary"]["referringDomains"] == 42 and out["cached"] is True


def test_estimate_report_is_not_labelled_live_and_does_not_invent_mentions():
    from app import ai_visibility

    report = ai_visibility._fallback_report({"business": "", "services": ""}, ["who fixes roofs"], "https://example.com", "OpenAI is not configured.")
    assert report["live"] is False and report["estimate"] is True
    assert all(check["mention"] is None and check["citation"] is None for check in report["checks"])
    assert all(engine["score"] is None for engine in report["engines"])
    assert all(row[2] == "Not estimated" for row in report["rows"])
