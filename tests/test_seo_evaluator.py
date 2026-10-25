"""Controlled cases for the independent metadata evaluator.

These fixtures are not a client's live site. null-like gaps stay unknown.
"""

from app.meta_writer import ctr_fit_score, seo_choice_score
from app.seo_evaluator import evaluate_metadata
from app.seo_evidence import build_evidence_record, normalize_page_url


PAGE = "https://example.com/categories/creative/"
BRAND = "Sovereign Standard"
OFFERS = ["digital marketing", "creative services"]
PAGE_TEXT = (
    "Sovereign Standard provides digital marketing and creative services in Calgary. "
    "The studio produces brand films, campaigns, and creative direction."
)


def _evidence(*, gsc_status="verified", queries=None, dfs_status="not_verified", title="Home", page_verified=True):
    return {
        "page_url": PAGE,
        "current_metadata": {"title": title, "description": "Current description."},
        "search_console": {
            "status": gsc_status,
            "property": "sc-domain:example.com",
            "queries": queries or [],
            "page_metrics": {"clicks": 12, "impressions": 340, "ctr": "3.5%", "position": "8.2"} if gsc_status == "verified" else None,
        },
        "analytics": {"status": "not_verified", "landing_page_metrics": None},
        "dataforseo": {"status": dfs_status, "page_keywords": [{"keyword": "creative services calgary"}] if dfs_status == "verified" else []},
        "technical_audit": {"status": "verified" if page_verified else "not_verified"},
        "data_quality_warnings": [],
    }


def test_choice_scores_rank_a_relevant_branded_title_above_a_thin_one():
    page = "Sovereign Standard provides creative services and digital marketing in Calgary."
    current = "Creative Services in Calgary | Sovereign Standard"
    thin_ctr = ctr_fit_score(
        "Home",
        "Welcome.",
        keyword="creative services",
        brand_tail="Sovereign Standard",
        current_title=current,
    )
    strong_ctr = ctr_fit_score(
        "Creative Services in Calgary | Sovereign Standard",
        "Sovereign Standard provides creative services and digital marketing for Calgary businesses ready to start a project.",
        keyword="creative services",
        brand_tail="Sovereign Standard",
        current_title=current,
        query_rows=[{"query": "creative services calgary", "impressions": 400}],
    )
    thin_seo = seo_choice_score("Home", "Welcome.", keyword="creative services", brand="Sovereign Standard", page_text=page, current_title=current)
    strong_seo = seo_choice_score(
        "Creative Services in Calgary | Sovereign Standard",
        "Sovereign Standard provides creative services and digital marketing for Calgary businesses.",
        keyword="creative services",
        brand="Sovereign Standard",
        page_text=page,
        current_title=current,
    )
    assert 1 <= thin_ctr <= 100 and 1 <= strong_ctr <= 100
    assert 1 <= thin_seo <= 100 and 1 <= strong_seo <= 100
    assert strong_ctr > thin_ctr
    assert strong_seo > thin_seo


def test_a_strong_current_title_is_kept():
    evidence = _evidence(
        title="Leading Digital Marketing & Creative Services in Calgary | Sovereign Standard",
        queries=[{"query": "creative services calgary", "impressions": 340}],
    )
    result = evaluate_metadata(
        evidence=evidence,
        current_title=evidence["current_metadata"]["title"],
        current_description="Calgary creative studio.",
        candidates=[{
            "title": "Creative Services Calgary",
            "description": "A shorter title with less context than the live listing.",
        }],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services calgary",
    )
    assert result["decision"] == "KEEP_CURRENT"
    assert result["recommended_title"] is None


def test_b_missing_search_console_does_not_fabricate_insights():
    evidence = _evidence(gsc_status="not_verified", queries=[], title="Creative Services | Sovereign Standard")
    result = evaluate_metadata(
        evidence=evidence,
        current_title=evidence["current_metadata"]["title"],
        current_description="Studio description from the page.",
        candidates=[{
            "title": "Creative Services | 1,200 monthly searches",
            "description": "This page ranks for 1,200 monthly searches in Calgary.",
        }],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services",
    )
    assert result["decision"] == "INSUFFICIENT_EVIDENCE"
    assert result["recommended_title"] is None
    assert "1,200" not in result["comparison_summary"]


def test_c_verified_query_can_replace_a_generic_title():
    evidence = _evidence(
        title="Home",
        queries=[{"query": "creative services calgary", "impressions": 900, "clicks": 40, "ctr": "4.4%", "position": "6.0"}],
    )
    result = evaluate_metadata(
        evidence=evidence,
        current_title="Home",
        current_description="Welcome.",
        candidates=[{
            "title": "Creative Services in Calgary | Sovereign Standard",
            "description": "Sovereign Standard provides digital marketing and creative services in Calgary.",
        }],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services calgary",
    )
    assert result["decision"] == "RECOMMEND_CHANGE"
    assert "Sovereign Standard" in (result["recommended_title"] or "")
    assert "creative" in (result["recommended_title"] or "").lower()


def test_d_brand_removal_is_rejected():
    current = "Leading Digital Marketing & Creative Services in Calgary | Sovereign Standard"
    evidence = _evidence(title=current, queries=[{"query": "creative services calgary", "impressions": 100}])
    result = evaluate_metadata(
        evidence=evidence,
        current_title=current,
        current_description="Calgary studio.",
        candidates=[{
            "title": "Leading Digital Marketing & Creative Services in Calgary",
            "description": "Digital marketing and creative services in Calgary.",
        }],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services",
    )
    assert result["decision"] == "KEEP_CURRENT"
    assert result["recommended_title"] is None
    assert any(item["outcome"] == "REJECT_CANDIDATE" for item in result["rejected"])
    assert any("brand" in " ".join(item["reasons"]).lower() for item in result["rejected"])


def test_e_duplicate_titles_are_flagged():
    current = "Creative Services in Calgary | Sovereign Standard"
    other = "Creative Services in Calgary | Sovereign Standard"
    evidence = _evidence(title="Brand Films | Sovereign Standard", queries=[{"query": "brand films calgary", "impressions": 80}])
    result = evaluate_metadata(
        evidence=evidence,
        current_title="Brand Films | Sovereign Standard",
        current_description="Brand films from the Calgary studio.",
        candidates=[{"title": other, "description": "Creative services in Calgary from Sovereign Standard."}],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services",
        taken_titles=[current],
    )
    assert result["recommended_title"] is None
    assert any("duplicates" in " ".join(item["reasons"]).lower() for item in result["rejected"])


def test_f_mismatched_search_console_blocks_the_change():
    evidence = _evidence(gsc_status="mismatched", title="Creative Services | Sovereign Standard")
    result = evaluate_metadata(
        evidence=evidence,
        current_title="Creative Services | Sovereign Standard",
        current_description="Calgary studio.",
        candidates=[{
            "title": "Creative Services in Calgary | Sovereign Standard",
            "description": "Sovereign Standard provides creative services in Calgary.",
        }],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services",
    )
    assert result["decision"] == "INSUFFICIENT_EVIDENCE"
    assert result["recommended_title"] is None


def test_g_unsupported_service_is_rejected():
    evidence = _evidence(
        title="Creative Services | Sovereign Standard",
        queries=[{"query": "creative services calgary", "impressions": 50}],
    )
    result = evaluate_metadata(
        evidence=evidence,
        current_title="Creative Services | Sovereign Standard",
        current_description="Calgary studio.",
        candidates=[{
            "title": "Emergency Plumbing in Calgary | Sovereign Standard",
            "description": "24/7 emergency plumbing with licensed plumbers and same-day repairs.",
        }],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services",
    )
    assert result["decision"] != "RECOMMEND_CHANGE"
    assert result["recommended_title"] is None
    assert result["rejected"]


def test_h_no_passing_candidate_returns_zero_recommendations():
    evidence = _evidence(title="Creative Services | Sovereign Standard", gsc_status="not_verified", queries=[])
    result = evaluate_metadata(
        evidence=evidence,
        current_title="Creative Services | Sovereign Standard",
        current_description="Calgary studio.",
        candidates=[
            {"title": "Welcome", "description": "Click here to learn more about our solutions."},
            {"title": "", "description": ""},
        ],
        brand=BRAND,
        offers=OFFERS,
        page_text=PAGE_TEXT,
        keyword="creative services",
    )
    assert result["accepted_count"] == 0
    assert result["recommended_title"] is None
    assert result["recommended_description"] is None
    assert result["decision"] in {"KEEP_CURRENT", "INSUFFICIENT_EVIDENCE"}


def test_evidence_record_keeps_property_separate_and_unknown_queries():
    record = build_evidence_record(
        page_url="https://www.example.com/categories/creative/",
        canonical_url=None,
        current_title="Creative",
        current_description="",
        gsc={"clicks": "—", "impressions": "—", "property": "sc-domain:example.com"},
        page={"url": PAGE, "text": PAGE_TEXT},
    )
    assert normalize_page_url(record["page_url"]) == normalize_page_url(PAGE)
    assert record["search_console"]["property"] == "sc-domain:example.com"
    assert record["search_console"]["property"] != record["page_url"]
    assert record["search_console"]["queries"] == []
    assert record["search_console"]["status"] == "not_verified"
    assert any("not proof" in warning.lower() or "empty" in warning.lower() for warning in record["data_quality_warnings"])
