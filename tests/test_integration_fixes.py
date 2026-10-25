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
