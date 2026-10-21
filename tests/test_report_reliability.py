"""Honest reporting: host isolation, keyword relevance, AI estimate labeling, audit counts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import ai_visibility, page_quality as pq, research
from app.listing_research import (
    best_keyword_from_research,
    keyword_fits_page,
    load_stored,
    research_lines,
    visibility_gap,
)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(research, "cached", lambda *a, **k: None)
    monkeypatch.setattr(research, "_save", lambda *a, **k: None)
    monkeypatch.setattr(research, "_budget", lambda *a, **k: None)
    monkeypatch.setattr(research, "_spend", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "plan_for", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)
    return []


def test_keyword_rejects_irrelevant_high_volume_term():
    stored = {
        "keywords": {
            "ranked": [
                {
                    "keyword": "huey lam suits",
                    "url": "https://agency.example/case-studies/huey",
                    "volume": 9000,
                    "position": 2,
                },
                {
                    "keyword": "calgary web design",
                    "url": "https://agency.example/",
                    "volume": 240,
                    "position": 12,
                },
            ],
            "ideas": [{"keyword": "menswear calgary", "volume": 5000}],
        }
    }
    best, alts = best_keyword_from_research(
        stored,
        url="https://agency.example/",
        topic="Web design agency Calgary",
        offers=["web design", "SEO"],
        places=["Calgary"],
        page_text="Agency homepage for web design and SEO in Calgary. Selected work includes projects.",
    )
    assert "huey" not in best.lower()
    assert "menswear" not in best.lower()
    assert all("huey" not in a.lower() and "menswear" not in a.lower() for a in alts)
    assert keyword_fits_page(
        "calgary web design", topic="Web design", page_text="web design calgary",
        offers=["web design"], places=["Calgary"],
    )
    assert not keyword_fits_page(
        "huey lam suits", topic="Web design", page_text="web design calgary",
        offers=["web design"], places=["Calgary"],
    )
    assert not keyword_fits_page(
        "menswear calgary", topic="Web design agency Calgary",
        offers=["web design"], places=["Calgary"],
    )

def test_missing_volume_is_not_treated_as_zero_demand():
    stored = {
        "keywords": {
            "ranked": [
                {"keyword": "web design calgary", "url": "https://agency.example/", "volume": None, "position": 8},
                {"keyword": "unrelated roofing", "url": "https://agency.example/other", "volume": 0, "position": 3},
            ]
        }
    }
    best, _ = best_keyword_from_research(
        stored,
        url="https://agency.example/",
        topic="web design",
        offers=["web design"],
        page_text="Calgary web design studio",
    )
    assert "web design" in best.lower()


def test_load_stored_scopes_by_host():
    class Row:
        def __init__(self, payload):
            self.payload = payload

    rows_by_kind = {
        "dfs-keywords": [
            Row({"host": "other.com", "ranked": [{"keyword": "wrong site"}], "ideas": []}),
            Row({
                "host": "agency.example",
                "ranked": [{"keyword": "web design", "url": "https://agency.example/", "volume": 100}],
                "ideas": [],
            }),
        ],
        "dfs-backlinks": [
            Row({"host": "other.com", "summary": {"referringDomains": 99}}),
            Row({"host": "agency.example", "summary": {"referringDomains": 12}, "fetchedAt": "2026-01-01"}),
        ],
        "dfs-audit": [
            Row({"host": "other.com", "issues": [{"name": "wrong"}]}),
            Row({"host": "agency.example", "report": {"issues": [{"name": "missing title", "fix": "Add a title"}]}}),
        ],
        "dfs-visibility": [],
        "site-audit": [Row({"findings": [{"name": "psi only"}]})],
    }

    class KindDB:
        def query(self, _model):
            self._kind = None
            return self

        def filter(self, *args, **_kwargs):
            for arg in args:
                right = getattr(arg, "right", None)
                val = getattr(right, "value", None) if right is not None else None
                if isinstance(val, str) and val in rows_by_kind:
                    self._kind = val
            blob = " ".join(str(a) for a in args)
            for kind in rows_by_kind:
                if f"'{kind}'" in blob or f'"{kind}"' in blob or kind in blob:
                    self._kind = kind
            return self

        def order_by(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def all(self):
            return list(rows_by_kind.get(self._kind, []))

        def first(self):
            rows = self.all()
            return rows[0] if rows else None

    out = load_stored(KindDB(), user_id=1, site="https://agency.example/")
    assert out["keywords"].get("host") == "agency.example"
    assert out["backlinks"]["summary"]["referringDomains"] == 12
    assert out["audit"]["report"]["issues"][0]["name"] == "missing title"
    assert "psi" not in str(out["audit"]).lower()

    empty = load_stored(KindDB(), user_id=1, site="https://unknown.example/")
    assert empty["keywords"] == {}
    assert empty["backlinks"] == {}


def test_research_lines_label_dfs_not_pagespeed():
    text = research_lines(
        {
            "keywords": {"ranked": []},
            "backlinks": {},
            "visibility": [{"engine": "ChatGPT", "mention": None, "snippet": ""}],
            "audit": {"report": {"issues": [{"name": "Empty title", "fix": "Write a title"}]}},
        },
        "https://agency.example/",
        "web design",
    )
    assert "DataForSEO on-page" in text
    assert "not mentioned" not in text.lower() or "not checked" in text.lower()
    assert "brand not checked" in text.lower() or "not checked" in text.lower()


def test_visibility_gap_distinguishes_unchecked():
    assert "not determined" in visibility_gap({"visibility": [{"mention": None, "engine": "ChatGPT"}]}).lower()
    assert "not named" in visibility_gap({"visibility": [{"mention": False, "engine": "ChatGPT"}]}).lower()


def test_ai_visibility_missing_mention_stays_not_estimated():
    checks = ai_visibility._normalize_checks(
        [{"engine": "ChatGPT", "prompt": "Who offers web design in Calgary?", "excerpt": "unclear"}],
        ["Who offers web design in Calgary?"],
        {"business": "Agency", "services": "web design", "areas": "Calgary"},
        "https://agency.example/",
    )
    chat = next(c for c in checks if c["engine"] == "ChatGPT")
    assert chat["mention"] is None
    assert chat["citation"] is None
    assert chat["status"] == "not_checked"
    assert chat.get("estimate") is True
    assert chat.get("observed") is False
    packed = ai_visibility._pack(
        {"business": "Agency", "services": "web design", "areas": "Calgary"},
        ["Who offers web design?"],
        [ai_visibility._score_engine(checks, "ChatGPT")],
        checks,
        model="test",
        note="estimate only",
    )
    assert packed["live"] is False and packed["estimate"] is True
    assert "Not a confirmed" in packed["disclaimer"] or "not queried live" in packed["disclaimer"].lower()
    assert any(row[2] == "Not estimated" for row in packed["rows"] if row[1] == "ChatGPT")


def test_audit_report_does_not_invent_blank_page_urls(offline, monkeypatch):
    summary = {
        "page_metrics": {"onpage_score": 50, "checks": {"no_title": 3}},
        "crawl_status": {"pages_crawled": 0, "max_crawl_pages": 10},
        "domain_info": {"name": "acme.ca", "checks": {"ssl": True, "sitemap": True, "robots_txt": True, "test_https_redirect": True}},
    }

    def fake_call(method, path, payload=None, timeout=60):
        if path.endswith("/on_page/pages"):
            return [{"items": []}], 0.0
        if "links" in path:
            return [{"items": []}], 0.0
        return [{}], 0.0

    monkeypatch.setattr(research, "_call", fake_call)
    monkeypatch.setattr(research, "_fetch_checked_pages", lambda *a, **k: [])
    report = research._audit_report("task-empty", summary)
    no_title = next(i for i in report["issues"] if i["key"] == "no_title")
    assert no_title["count"] == 3
    assert no_title["pages"] == []
    assert no_title.get("urlsMissing") is True
    assert no_title.get("countSource") == "summary_metric"
    assert not any(p.get("url") == "" for p in no_title["pages"])


def test_own_crawl_seed_is_not_google_live():
    import inspect

    from app.routers import crawl as crawl_mod

    src = inspect.getsource(crawl_mod.run_crawl)
    assert "own-crawl-v1" in src
    assert "google-live-v1" not in src


def test_confidence_lists_missing_evidence():
    out = pq.confidence(content="thin", gsc={}, rivals=[], validation="unverified")
    assert out["level"] == "low"
    assert out["missing"]
    assert "rankings" in out["note"].lower() or "traffic" in out["note"].lower()


def test_homepage_retain_when_draft_not_stronger():
    """Accurate live homepage metadata should not be replaced by a weaker rewrite."""
    from app.meta_writer import write_title_description

    page = {
        "url": "https://agency.example/",
        "title": "Agency Example | Web Design, SEO & Digital Marketing Agency in Calgary",
        "description": "Agency Example helps businesses grow through professional web design, SEO, Google Ads, branding, digital marketing and creative production.",
        "h1": "Home",
        "headings": "Selected Work | Services",
        "text": "Selected work: Huey Lam Suits case study. We offer web design and SEO in Calgary.",
        "words": 40,
        "ogTitle": "",
        "ogDescription": "",
        "schemaName": "",
        "schemaDescription": "",
        "schemaTypes": [],
        "nav": [],
        "faqs": [],
        "videos": [],
        "images": [],
        "canonical": "https://agency.example/",
    }
    # Near-copy of live title — intentionally not stronger.
    weak = (
        "TITLE: Agency Example | Web Design, SEO & Digital Marketing Agency in Calgary\n"
        "DESCRIPTION: Agency Example helps businesses grow through professional web design, SEO, Google Ads, branding, digital marketing and creative production.\n"
        "TARGET: web design\n"
        "REASON: Same as live.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(weak, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=page),
        patch("app.dfs_meta.draft_from_research", return_value={}),
        patch("app.meta_writer.listing_is_stronger", return_value=False),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile={"business": "Agency Example", "services": "web design, SEO", "areas": "Calgary"},
            page=page,
            site={"business": {"brand": "Agency Example", "offers": ["web design", "SEO"], "places": ["Calgary"]}},
            kind="home",
        )
    assert "huey" not in f"{out['title']} {out['metaDescription']}".lower()
    assert out["beatsLive"] is False
    assert "keeping the current" in (out.get("reason") or "").lower() or out["title"] == page["title"][:60] or "Agency Example" in out["title"]
