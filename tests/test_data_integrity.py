"""Production data-integrity: no fake SEO metrics, no demo leakage, honest unavailable states."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app import ai_visibility, enrichment, provenance
from app.listing_research import keyword_fits_page, load_stored
from app.routers.features import ensure_screen_rows


def test_ensure_screen_rows_is_noop():
    db = MagicMock()
    user = MagicMock(id=1)
    ensure_screen_rows(db, user)
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_keyword_fits_page_rejects_empty_context():
    assert keyword_fits_page("seo audit tool", topic="", page_text="", offers=[]) is False


def test_keyword_fits_page_accepts_page_present_term_with_low_offer_overlap():
    page = (
        "We specialize in visual website design, UI/UX, and brand systems. "
        "Development is offered separately on another page."
    )
    assert keyword_fits_page(
        "UI UX design",
        topic="visual website design",
        page_text=page,
        offers=["web design and development"],
    ) is True


def test_load_stored_requires_site_host():
    class EmptyDB:
        def query(self, *_a, **_k):
            raise AssertionError("must not query without a site host")

    out = load_stored(EmptyDB(), user_id=1, site="")
    assert out["keywords"] == {}
    assert out["backlinks"] == {}
    assert out["visibility"] == []
    assert out["audit"] == {}


def test_provenance_rejects_catalog_seed():
    assert provenance.is_live_payload({"seedVersion": "2026-09-24-multi-v1", "rows": [["x"]]}) is False
    assert provenance.is_live_payload({"seedVersion": "google-live-v1", "source": "google_gsc"}) is True
    assert provenance.is_measured_payload({"seedVersion": "operator-estimate-v1", "estimate": True}) is False


def test_ai_visibility_pack_uses_estimate_seed_not_google_live():
    report = ai_visibility._pack(
        {"business": "Acme", "services": "plumbing", "areas": "Austin"},
        ["Who offers plumbing in Austin?"],
        [],
        [],
        model="rules",
        note="test",
    )
    assert report["seedVersion"] == ai_visibility.ESTIMATE
    assert report["seedVersion"] != ai_visibility.LIVE
    assert report["estimate"] is True
    assert report["live"] is False
    assert report["observed"] is False
    assert "not a" in report["disclaimer"].lower() or "were not queried" in report["disclaimer"].lower()


def test_enrichment_does_not_overwrite_ai_visibility_report():
    calls = []

    def fake_upsert(_db, _uid, kind, title, payload):
        calls.append((kind, title, payload))

    with (
        patch("app.enrichment.openai_configured", return_value=True),
        patch("app.enrichment.chat_text", return_value=("Summary line\nOpportunities\n", "gpt-test", "writer")),
        patch("app.enrichment._latest_rows", side_effect=lambda *_a, **_k: [["plumbing austin", 10, 100]] if "organic" in str(_a) or True else []),
        patch("app.enrichment._upsert", side_effect=fake_upsert),
        patch("app.enrichment._parse_table", return_value=[["Cluster", "q", "Informational", "High"]]),
    ):
        # Drive only the narrative block by stubbing earlier sections lightly via parse results.
        enrichment.enrich_from_google(MagicMock(), user_id=1)

    kinds = [c[0] for c in calls]
    assert "ai-visibility-report" not in kinds
    assert "ai-analysis" in kinds or "brand-narrative" in kinds or "topic-research" in kinds


def test_enrichment_does_not_invent_guide_keywords_when_model_empty():
    stored = {}

    def fake_upsert(_db, _uid, kind, title, payload):
        stored[kind] = payload

    def fake_chat(**kwargs):
        kind = kwargs.get("kind") or ""
        if kind == "generate-keywords":
            return ("sorry cannot", "gpt-test", "writer")
        if kind == "topic-research":
            return ("Cluster | Sample | Intent | Priority\nPlumbing | plumbing austin | Informational | High", "gpt-test", "writer")
        if kind == "prompt-research":
            return ("", "gpt-test", "writer")
        if kind == "ai-analysis":
            return ("Summary: owned data only.", "gpt-test", "writer")
        return ("", "gpt-test", "writer")

    with (
        patch("app.enrichment.openai_configured", return_value=True),
        patch("app.enrichment.chat_text", side_effect=fake_chat),
        patch(
            "app.enrichment._latest_rows",
            side_effect=lambda _db, _uid, kind: (
                [["plumbing austin", "10", "100"]] if kind == "organic-search" else [["https://acme.example/", "5"]]
            ),
        ),
        patch("app.enrichment._upsert", side_effect=fake_upsert),
    ):
        enrichment.enrich_from_google(MagicMock(), user_id=1)

    kw = stored.get("generate-keywords") or {}
    rows = kw.get("rows") or []
    assert rows
    assert all("guide" not in str(row[0]).lower() for row in rows)
    assert "ai-visibility-report" not in stored


def test_latest_visibility_rejects_gsc_narrative_as_measurement():
    class FakeDB:
        def query(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def order_by(self, *_a, **_k):
            return self

        def first(self):
            row = MagicMock()
            row.payload = {
                "seedVersion": "google-live-v1",
                "source": "gsc_ga4_openai",
                "dataKind": "gsc_ga4_narrative",
                "summary": "Period-style narrative",
                "draft": "text",
            }
            return row

    out = ai_visibility.latest_report(FakeDB(), user_id=1)
    assert out.get("status") == "unavailable" or out.get("measured") is False
    assert "narrative" in (out.get("disclaimer") or "").lower() or out.get("dataKind") == "gsc_ga4_narrative"
