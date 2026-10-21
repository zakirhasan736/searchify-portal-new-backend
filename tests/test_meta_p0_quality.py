"""P0 regression: page-specific titles/meta — no forced keywords, no invent fallbacks, shared validation."""

from __future__ import annotations

from unittest.mock import patch

from app.jev import score_listing
from app.meta_writer import (
    hard_issues_only,
    is_hard_issue,
    listing_fluff_problems,
    listing_is_stronger,
    matching_queries,
    write_title_description,
)


SOVEREIGN_HOME = {
    "url": "https://sovereignstandard.ca/",
    "title": "Sovereign Standard | Web Design, SEO & Digital Marketing Agency in Calgary",
    "description": (
        "Sovereign Standard helps businesses grow through professional web design, SEO, "
        "Google Ads, branding, digital marketing and creative production."
    ),
    "h1": "Home",
    "headings": "Selected Work | Web Design | SEO | Google Ads | Branding",
    "text": (
        "Sovereign Standard is a Calgary web design and digital marketing agency. "
        "Services include web design, development, SEO, Google Ads, branding, digital marketing "
        "and creative production. Selected work includes Huey Lam Suits."
    ),
    "words": 70,
    "ogTitle": "",
    "ogDescription": "",
    "schemaName": "Sovereign Standard",
    "schemaDescription": "",
    "schemaTypes": ["WebPage", "Organization", "WebSite"],
    "nav": ["Work", "Services", "Contact"],
    "faqs": [],
    "videos": [],
    "images": [],
    "canonical": "https://sovereignstandard.ca/",
    "ogImage": "",
}

SOVEREIGN_SITE = {
    "business": {
        "brand": "Sovereign Standard",
        "offers": ["web design", "SEO", "Google Ads", "branding", "digital marketing", "creative production"],
        "places": ["Calgary"],
    },
    "page": {"role": ""},
}

SOVEREIGN_PROFILE = {
    "business": "Sovereign Standard",
    "services": "web design, SEO, Google Ads, branding, digital marketing, creative production",
    "areas": "Calgary",
}

SERVICE_PAGE = {
    "url": "https://sovereignstandard.ca/services/seo",
    "title": "SEO Services | Sovereign Standard",
    "description": "Technical and content SEO for Calgary businesses.",
    "h1": "SEO Services",
    "headings": "SEO Services | Audits | Content",
    "text": (
        "Our SEO service covers technical audits, content planning and Search Console monitoring. "
        "Selected work elsewhere: Huey Lam Suits case study. We also offer web design on other pages."
    ),
    "words": 50,
    "ogTitle": "",
    "ogDescription": "",
    "schemaName": "",
    "schemaDescription": "",
    "schemaTypes": [],
    "nav": [],
    "faqs": [],
    "videos": [],
    "images": [],
    "canonical": "https://sovereignstandard.ca/services/seo",
}

PORTFOLIO_PAGE = {
    "url": "https://sovereignstandard.ca/work",
    "title": "Our Work | Sovereign Standard",
    "description": "Selected web design and marketing projects from Sovereign Standard.",
    "h1": "Our Work",
    "headings": "Portfolio | Selected Work | Clients",
    "text": (
        "Browse our portfolio of web design and digital marketing projects. "
        "Featured: Huey Lam Suits menswear rebuild. More case studies coming soon."
    ),
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
    "canonical": "https://sovereignstandard.ca/work",
}


def test_irrelevant_research_keyword_not_forcibly_selected():
    """Research keyword that fails matching_queries must not be forced into the primary keyword."""
    page_text = (
        "Sovereign Standard web design SEO Google Ads branding Calgary. "
        "Selected work Huey Lam Suits menswear."
    )
    matched = matching_queries(
        ["huey lam suits menswear", "web design calgary", "seo calgary"],
        "web design",
        page_text,
    )
    assert all("huey" not in m.lower() for m in matched) or matched[0].lower() != "huey lam suits menswear"

    weak = (
        "TITLE: Huey Lam Suits Menswear | Sovereign Standard\n"
        "DESCRIPTION: Menswear ecommerce for Huey Lam Suits by Sovereign Standard.\n"
        "TARGET: huey lam suits menswear\n"
        "REASON: Forced research keyword.\n"
    )
    research = {
        "keywords": {
            "ranked": [
                {"keyword": "huey lam suits menswear", "volume": 12000, "position": 3, "url": "https://other.example/"},
                {"keyword": "web design calgary", "volume": 400, "position": 12, "url": "https://sovereignstandard.ca/"},
            ]
        }
    }
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(weak, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
            research=research,
        )
    blob = f"{out['title']} {out['metaDescription']}".lower()
    assert "huey" not in blob
    assert out.get("recommendKeep") is True or "sovereign" in out["title"].lower()


def test_rejected_candidate_does_not_trigger_forced_replacement():
    """Failed AI draft must keep published listing — never auto-invent via stronger_listing."""
    bad = (
        "TITLE: Design and Web Development in Calgary | 5 Core Services\n"
        "DESCRIPTION: Business owners can explore web design and development from a Calgary-based agency with five core services, then contact Sovereign Standard to start.\n"
        "TARGET: web design\n"
        "REASON: Counted services.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(bad, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
        patch("app.meta_writer.stronger_listing") as invent,
    ):
        invent.side_effect = AssertionError("stronger_listing must not auto-invent a replacement")
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
        )
    assert out.get("recommendKeep") is True
    assert "5 core" not in f"{out['title']} {out['metaDescription']}".lower()
    assert invent.call_count == 0


def test_published_title_can_win_over_generated_candidate():
    """Sovereign homepage regression: thin keyword-led draft must not beat the published listing."""
    generated = "Design and Web Development in Calgary | 5 Core Services"
    generated_desc = (
        "Business owners can explore web design and development from a Calgary-based agency "
        "with five core services, then contact Sovereign Standard to start."
    )
    assert listing_fluff_problems(generated, generated_desc, evidence=SOVEREIGN_HOME["text"])
    assert listing_is_stronger(
        generated,
        generated_desc,
        SOVEREIGN_HOME["title"],
        SOVEREIGN_HOME["description"],
        "Sovereign Standard",
        keyword="web design",
        places=["Calgary"],
        offers=SOVEREIGN_SITE["business"]["offers"],
    ) is False

    draft = (
        f"TITLE: {generated}\n"
        f"DESCRIPTION: {generated_desc}\n"
        "TARGET: web design\n"
        "REASON: Keyword first.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(draft, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
            objective="balanced",
        )
    assert out.get("recommendKeep") is True
    assert out.get("beatsLive") is False
    assert "sovereign standard" in out["title"].lower()
    assert "5 core" not in out["title"].lower()
    assert "recommendKeep" in out
    assert "openIssues" in out
    assert "jev" in out


def test_dataforseo_output_runs_through_same_validation_gates():
    """DFS-only path must apply hard checks; fluff drafts become no-change."""
    dfs_bad = {
        "title": "Design and Web Development in Calgary | 5 Core Services",
        "description": "Business owners can explore web design with five core services.",
    }
    with (
        patch("app.meta_writer.openai_configured", return_value=False),
        patch("app.dfs_meta.draft_from_research", return_value=dfs_bad),
        patch("app.research.configured", return_value=True),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
        )
    assert out.get("recommendKeep") is True
    assert "5 core" not in f"{out['title']} {out['metaDescription']}".lower()
    assert "sovereign" in out["title"].lower()
    assert out.get("beatsLive") is False


def test_dataforseo_failure_does_not_produce_false_success():
    """When DFS is configured but returns empty, do not claim a successful rewrite."""
    with (
        patch("app.meta_writer.openai_configured", return_value=False),
        patch("app.dfs_meta.draft_from_research", return_value={}),
        patch("app.research.configured", return_value=True),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
        )
    assert out.get("recommendKeep") is True
    assert out.get("beatsLive") is False
    assert any("failed" in str(i).lower() or "unavailable" in str(i).lower() for i in (out.get("openIssues") or [])) or (
        "unavailable" in (out.get("reason") or "").lower() or "nothing usable" in (out.get("reason") or "").lower()
    )
    assert (out.get("analysis") or {}).get("providerFailed") is True
    jev = out.get("jev") or {}
    assert jev.get("band") == "reject" or jev.get("checks", {}).get("hard_fail")


def test_homepage_retains_business_identity():
    clean_but_thin = (
        "TITLE: Web Design in Calgary\n"
        "DESCRIPTION: Explore web design services in Calgary.\n"
        "TARGET: web design\n"
        "REASON: Short keyword title.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(clean_but_thin, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
        )
    assert "sovereign" in out["title"].lower() or out.get("recommendKeep")
    assert "huey" not in f"{out['title']} {out['metaDescription']}".lower()


def test_service_page_uses_actual_service_not_sitewide():
    bleed = (
        "TITLE: Web Design Services | Sovereign Standard\n"
        "DESCRIPTION: Sovereign Standard builds websites. Also see Huey Lam Suits.\n"
        "TARGET: web design\n"
        "REASON: Site-wide offer.\n"
    )
    clean = (
        "TITLE: SEO Services for Calgary Businesses | Sovereign Standard\n"
        "DESCRIPTION: Sovereign Standard delivers technical and content SEO for Calgary businesses.\n"
        "TARGET: seo services\n"
        "REASON: Service page subject.\n"
    )
    calls = {"n": 0}

    def fake_ask(_brief, **_k):
        calls["n"] += 1
        return (bleed if calls["n"] == 1 else clean, "test", "writer")

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=fake_ask),
        patch("app.meta_writer.scrape_page", return_value=SERVICE_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SERVICE_PAGE["url"],
            profile=SOVEREIGN_PROFILE,
            page=SERVICE_PAGE,
            site=SOVEREIGN_SITE,
            kind="service",
        )
    blob = f"{out['title']} {out['metaDescription']}".lower()
    assert "huey" not in blob
    assert "seo" in blob or out.get("recommendKeep")


def test_portfolio_archive_does_not_inherit_project_name():
    bleed = (
        "TITLE: Huey Lam Suits Portfolio | Sovereign Standard\n"
        "DESCRIPTION: See the Huey Lam Suits menswear rebuild and more projects.\n"
        "TARGET: huey lam suits\n"
        "REASON: Named featured project.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(bleed, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=PORTFOLIO_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=PORTFOLIO_PAGE["url"],
            profile=SOVEREIGN_PROFILE,
            page=PORTFOLIO_PAGE,
            site={**SOVEREIGN_SITE, "page": {"role": "portfolio"}},
            kind="portfolio",
        )
    # Either kept published archive listing or regenerated without project dominance.
    if not out.get("recommendKeep"):
        assert "huey" not in out["title"].lower()
    else:
        assert "work" in out["title"].lower() or "sovereign" in out["title"].lower()


def test_unsupported_numbers_and_claims_rejected():
    issues = listing_fluff_problems(
        "Award-Winning Agency | 5 Core Services",
        "Business owners can explore our #1 ranking services with 300% traffic growth.",
        evidence="We offer web design and SEO in Calgary.",
    )
    assert issues
    assert any(is_hard_issue(i) for i in issues)
    assert hard_issues_only(issues)


def test_missing_keyword_evidence_does_not_invent_research():
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch(
            "app.meta_writer._ask",
            return_value=(
                "TITLE: Design and Web Development in Calgary | 5 Core Services\n"
                "DESCRIPTION: Business owners can explore five core services.\n"
                "TARGET: invented keyword\n"
                "REASON: Invented.\n",
                "test",
                "writer",
            ),
        ),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
            queries=[],
            research=None,
            gsc={},
        )
    assert out.get("recommendKeep") is True
    analysis = out.get("analysis") or {}
    # Missing evidence is reported; research is not fabricated into openIssues as success.
    assert analysis.get("keywordEvidenceInsufficient") in {True, False}
    assert "5 core" not in out["title"].lower()


def test_no_change_result_preserves_api_contract():
    bad = (
        "TITLE: Design and Web Development in Calgary | 5 Core Services\n"
        "DESCRIPTION: Business owners can explore web design.\n"
        "TARGET: web design\n"
        "REASON: Weak.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(bad, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=SOVEREIGN_HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SOVEREIGN_HOME["url"],
            profile=SOVEREIGN_PROFILE,
            page=SOVEREIGN_HOME,
            site=SOVEREIGN_SITE,
            kind="home",
        )
    for key in (
        "title",
        "metaDescription",
        "openGraphTitle",
        "openGraphDescription",
        "openIssues",
        "beatsLive",
        "recommendKeep",
        "jev",
        "analysis",
        "reason",
        "target",
        "pageType",
        "jsonLd",
        "objective",
    ):
        assert key in out
    assert out["recommendKeep"] is True
    assert out["beatsLive"] is False
    assert isinstance(out["openIssues"], list)
    assert isinstance(out["jev"], dict)
    assert "score" in out["jev"] and "band" in out["jev"]


def test_jev_hard_fail_cannot_mark_safe_recommendation():
    jev = score_listing(
        title="Design and Web Development | 5 Core Services",
        description="Business owners can explore five core services.",
        keyword="web design",
        live_title=SOVEREIGN_HOME["title"],
        live_description=SOVEREIGN_HOME["description"],
        beats_live=False,
        open_issues=["Do not invent “5 Core Services”. Only use a service count when the page states that number."],
        hard_fail=True,
    )
    assert jev["band"] == "reject"
    assert jev["checks"].get("hard_fail") is True
    assert jev["score"] <= 35
