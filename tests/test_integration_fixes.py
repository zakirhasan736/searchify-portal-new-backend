"""Regressions for the DataForSEO, Search Console, WordPress publish and image fixes."""

from __future__ import annotations

from unittest.mock import patch

from app import dfs_meta
from app.google_oauth import _gsc_page_variants, search_console_links
from app.meta_writer import _query_metric_rows, write_title_description
from app.page_scrape import _image_name


def test_generate_meta_tags_uses_creativity_index_and_fits_500_tokens():
    sent = {}

    def fake_call(method, path, payload, timeout=60):
        sent["payload"] = payload[0]
        return [{"title": "T", "description": "D"}], 0.01

    packed = dfs_meta.pack_source_text(keyword="roof repair", topic="Roof repair", page_text="word " * 3000)
    assert len(packed) <= dfs_meta.SOURCE_CHAR_LIMIT
    with patch("app.research._call", side_effect=fake_call):
        out = dfs_meta.generate_meta_tags(packed)
    assert out["title"] == "T"
    assert "creativity_index" in sent["payload"] and "creativity" not in sent["payload"]
    assert len(sent["payload"]["text"]) <= dfs_meta.SOURCE_CHAR_LIMIT


def test_page_bundle_queries_reach_the_writer():
    gsc = {"queries": [
        {"query": "emergency plumber calgary", "clicks": 3, "impressions": 900, "ctr": "0.3%", "position": "11.2"},
        {"query": "drain cleaning calgary", "clicks": 9, "impressions": 2400, "ctr": "0.4%", "position": "8.1"},
    ]}
    rows = _query_metric_rows(gsc, [])
    assert {r["query"] for r in rows} == {"emergency plumber calgary", "drain cleaning calgary"}
    page = {
        "url": "https://acme.example/drains", "title": "Drains | Acme", "description": "Drains.", "h1": "Drain cleaning",
        "headings": "", "text": "Acme offers drain cleaning in Calgary.", "words": 7, "schemaTypes": [], "images": [],
    }
    captured = {}

    def fake_ask(brief, **_k):
        captured["brief"] = brief
        return "TITLE: Drain Cleaning in Calgary | Acme\nDESCRIPTION: Drain cleaning for Calgary homes.\n", "t", "w"

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=fake_ask),
        patch("app.meta_writer.scrape_page", return_value=page),
    ):
        write_title_description(url=page["url"], profile={"business": "Acme"}, page=page, gsc=gsc,
                                queries=["acme"], site={"business": {"brand": "Acme", "offers": ["drain cleaning"], "places": ["Calgary"]}},
                                kind="service", content_seed={})
    assert "drain cleaning calgary · clicks 9 · impressions 2400" in captured["brief"]


def test_gsc_variants_cover_slash_and_www():
    assert set(_gsc_page_variants("https://acme.example/drains")) >= {
        "https://acme.example/drains", "https://acme.example/drains/", "https://www.acme.example/drains",
    }


def test_search_console_links_encode_property_and_page():
    links = search_console_links("sc-domain:acme.example", "https://acme.example/drains")
    assert "resource_id=sc-domain%3Aacme.example" in links["performance"]
    assert "inspect" in links and "pagePerformance" in links
    assert search_console_links("") == {}


def test_image_names_drop_camera_and_cms_noise():
    assert _image_name("https://a.example/wp-content/uploads/IMG_4433-1024x768-scaled.jpg") == ""
    assert _image_name("https://a.example/9f86d081884c7d659a2feaa0c55ad015.jpg") == ""
    assert _image_name("https://a.example/2024-05-01-roof-repair-calgary.jpg") == "Roof repair calgary"


def test_keyword_ideas_come_from_dataforseo_reading_the_site(monkeypatch):
    from app import research

    sent = []

    def fake_call(method, path, payload, timeout=60):
        sent.append((path, payload[0]))
        if "keywords_for_site" in path:
            return [{"items": [
                {"keyword": "drain cleaning austin", "keyword_info": {"search_volume": 900},
                 "search_intent_info": {"main_intent": "commercial"}},
                {"keyword": "how to unclog a drain", "keyword_info": {"search_volume": 4000},
                 "search_intent_info": {"main_intent": "informational"}},
            ]}], 0.0
        return [{"items": []}], 0.0

    monkeypatch.setattr(research, "_call", fake_call)
    monkeypatch.setattr(research, "_place_for", lambda *a, **k: (research.locations.Location(country="United States", iso="US", language="en"), {}))
    monkeypatch.setattr(research, "business_offers", lambda *a: [])
    monkeypatch.setattr(research, "cached", lambda *a, **k: None)
    for name in ("_budget", "_spend", "_save"):
        monkeypatch.setattr(research, name, lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "check", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "tracked_keyword_limit", lambda *a: None)
    monkeypatch.setattr(research.quotas, "keyword_idea_limit", lambda *a: 20)

    class U:
        id, role, plan = 1, "ROLE_CLIENT", "starter"

    out = research.keywords(None, U(), site="https://acmeplumbing.com", terms=[])
    paths = [path for path, _ in sent]
    assert "/dataforseo_labs/google/keywords_for_site/live" in paths
    assert not any("keyword_suggestions" in path for path in paths)
    body = next(b for p, b in sent if "keywords_for_site" in p)
    assert body["target"] == "acmeplumbing.com" and "keyword" not in body  # the site, not a seed
    assert [row["keyword"] for row in out["ideas"]] == ["drain cleaning austin", "how to unclog a drain"]  # DataForSEO relevance order
    assert out["ideas"][0]["seed"] == "commercial"  # grouped by DataForSEO intent
    assert out["seeds"] == []


def test_audit_reports_description_length_with_page_detail(monkeypatch):
    from app import research

    pages = [
        {"resource_type": "html", "url": "https://acme.ca/a", "status_code": 200, "checks": {},
         "meta": {"title": "A", "description": "x" * 190, "description_length": 190}},
        {"resource_type": "html", "url": "https://acme.ca/b", "status_code": 200, "checks": {},
         "meta": {"title": "B", "description": "Short one.", "description_length": 10}},
    ]
    monkeypatch.setattr(research, "_call", lambda method, path, payload, timeout=60: ([{"items": pages}], 0.0) if path == "/on_page/pages" else ([{"items": []}], 0.0))
    report = research._audit_report("t1", {"page_metrics": {}, "crawl_status": {}, "domain_info": {}})
    keys = {row["key"]: row for row in report["issues"]}
    assert keys["description_too_long"]["pages"][0]["detail"].startswith("Meta description is 190 characters")
    assert keys["description_too_short"]["pages"][0]["url"] == "https://acme.ca/b"
    assert keys["description_too_long"]["meta"] is True


def test_audit_crawl_loads_resources_for_image_script_css_weight(monkeypatch):
    from app import research

    sent = {}

    def fake_task(method, path, payload, timeout=60, ok=(20000,)):
        sent["payload"] = payload[0]
        return {"id": "t1"}, 0.0

    monkeypatch.setattr(research, "_task", fake_task)
    monkeypatch.setattr(research, "_record", lambda *a: None)
    for name in ("_budget", "_spend", "_save"):
        monkeypatch.setattr(research, name, lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "check", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)

    class U:
        id, role, plan = 1, "ROLE_CLIENT", "starter"

    research.audit(None, U(), site="https://acme.ca")
    assert sent["payload"]["load_resources"] is True
