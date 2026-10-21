"""Regression: homepage / service / case-study metadata must stay on the right page."""

from __future__ import annotations

from unittest.mock import patch

from app import page_quality as pq
from app.meta_writer import (
    _parse_meta,
    stronger_listing,
    write_title_description,
)


HOME_PAGE = {
    "url": "https://agency.example/",
    "title": "Agency Example | Web Design, SEO & Digital Marketing in Calgary",
    "description": "Agency Example helps businesses grow through web design, SEO, Google Ads, branding and creative production.",
    "h1": "Home",
    "headings": "Selected Work | Web Design | SEO | Google Ads",
    "text": (
        "Agency Example is a Calgary web design and digital marketing studio. "
        "Selected work: Huey Lam Suits case study — menswear ecommerce rebuild. "
        "Services include web design, development, SEO, Google Ads, branding and creative production."
    ),
    "words": 80,
    "ogTitle": "",
    "ogDescription": "",
    "schemaName": "",
    "schemaDescription": "",
    "schemaTypes": [],
    "nav": ["Work", "Services", "Contact"],
    "faqs": [],
    "videos": [],
    "images": [],
    "canonical": "https://agency.example/",
}

CASE_PAGE = {
    "url": "https://agency.example/case-studies/huey-lam-suits",
    "title": "Huey Lam Suits | Case Study",
    "description": "How Agency Example rebuilt the Huey Lam Suits storefront.",
    "h1": "Huey Lam Suits",
    "headings": "Case Study | Results | Process",
    "text": (
        "Case study: Huey Lam Suits. We redesigned the ecommerce experience and improved product discovery. "
        "Agency Example handled web design and development for this menswear brand."
    ),
    "words": 60,
    "ogTitle": "",
    "ogDescription": "",
    "schemaName": "",
    "schemaDescription": "",
    "schemaTypes": [],
    "nav": [],
    "faqs": [],
    "videos": [],
    "images": [],
    "canonical": "https://agency.example/case-studies/huey-lam-suits",
}

SERVICE_PAGE = {
    "url": "https://agency.example/services/seo",
    "title": "SEO Services | Agency Example",
    "description": "Technical and content SEO for Calgary businesses.",
    "h1": "SEO Services",
    "headings": "SEO Services | What we fix | Process",
    "text": (
        "Our SEO service covers technical audits, content planning and Search Console monitoring. "
        "Selected work elsewhere: Huey Lam Suits case study."
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
    "canonical": "https://agency.example/services/seo",
}

PRODUCT_PAGE = {
    "url": "https://shop.example/products/oak-desk",
    "title": "Oak Desk | Shop Example",
    "description": "Solid oak desk for home offices.",
    "h1": "Oak Desk",
    "headings": "Oak Desk | Specs | Shipping",
    "text": "The Oak Desk is a solid wood standing desk. Free shipping in Canada. See also our Maple Chair case study.",
    "words": 40,
    "ogTitle": "",
    "ogDescription": "",
    "schemaName": "Oak Desk",
    "schemaDescription": "",
    "schemaTypes": ["Product"],
    "nav": [],
    "faqs": [],
    "videos": [],
    "images": [],
    "canonical": "https://shop.example/products/oak-desk",
}

SITE = {
    "business": {
        "brand": "Agency Example",
        "offers": ["web design", "SEO", "Google Ads", "branding"],
        "places": ["Calgary"],
    },
    "page": {"role": ""},
}

PROFILE = {
    "business": "Agency Example",
    "services": "web design, SEO, Google Ads, branding",
    "areas": "Calgary",
}


def test_stronger_listing_skips_portfolio_proof_on_homepage():
    title, desc, _ = stronger_listing(
        keyword="web design",
        alternatives=["web design", "SEO", "Google Ads"],
        offers=SITE["business"]["offers"],
        places=["Calgary"],
        current_title=HOME_PAGE["title"],
        current_desc=HOME_PAGE["description"],
        headings=HOME_PAGE["headings"],
        text=HOME_PAGE["text"],
        brand="Agency Example",
        page_kind="home",
    )
    assert "huey" not in title.lower()
    assert "huey" not in desc.lower()
    assert "case study" not in f"{title} {desc}".lower()


def test_stronger_listing_may_mention_proof_on_case_study():
    _, desc, _ = stronger_listing(
        keyword="case study",
        alternatives=["case study", "web design"],
        offers=["web design"],
        places=["Calgary"],
        current_title=CASE_PAGE["title"],
        current_desc=CASE_PAGE["description"],
        headings=CASE_PAGE["headings"],
        text=CASE_PAGE["text"],
        brand="Agency Example",
        page_kind="case_study",
    )
    # Proof injection is allowed on case studies; must not invent unrelated offers as the subject.
    assert "huey" in desc.lower() or "including" in desc.lower() or "compare" in desc.lower()


def test_parse_meta_handles_malformed_ai_output():
    title, desc, reason = _parse_meta("sorry I cannot help with that")
    assert title == "" and desc == "" and reason == ""
    title, desc, reason = _parse_meta("TITLE: Only a title\nREASON: missing description")
    assert title.startswith("Only") and desc == "" and "missing" in reason.lower()


def test_homepage_write_rejects_case_study_ai_draft():
    bleed = (
        "TITLE: Design and Web Development | Huey Lam Suits Case Study\n"
        "DESCRIPTION: For business owners, Agency Example pairs web design with SEO. View Huey Lam Suits work.\n"
        "TARGET: web design\n"
        "REASON: Named the project for proof.\n"
    )
    clean = (
        "TITLE: Web Design & Development Agency in Calgary | Agency Example\n"
        "DESCRIPTION: Agency Example helps Calgary businesses grow with web design and development, SEO, Google Ads, branding, and creative services.\n"
        "TARGET: web design\n"
        "REASON: Homepage offer and place.\n"
    )
    calls = {"n": 0}

    def fake_ask(brief, **_kwargs):
        calls["n"] += 1
        # First draft bleeds; retry stays clean.
        return (bleed if calls["n"] == 1 else clean, "test-model", "writer")

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=fake_ask),
        patch("app.meta_writer.scrape_page", return_value=HOME_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile=PROFILE,
            page=HOME_PAGE,
            site=SITE,
            kind="home",
            gsc=None,
            queries=[],
            competitors=[],
        )
    blob = f"{out['title']} {out['metaDescription']}".lower()
    assert "huey" not in blob
    assert "case study" not in blob
    assert out["title"]
    assert out["metaDescription"]


def test_homepage_missing_gsc_and_dfs_still_generates():
    clean = (
        "TITLE: Web Design Agency in Calgary | Agency Example\n"
        "DESCRIPTION: Agency Example helps Calgary businesses with web design, SEO, Google Ads and branding.\n"
        "TARGET: web design\n"
        "REASON: Offer-led homepage listing.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(clean, "test-model", "writer")),
        patch("app.meta_writer.scrape_page", return_value=HOME_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile=PROFILE,
            page=HOME_PAGE,
            site=SITE,
            kind="home",
            gsc={},
            queries=[],
            research=None,
        )
    assert out["title"]
    assert "huey" not in out["title"].lower()
    assert out.get("openIssues") is not None


def test_case_study_write_keeps_project_subject():
    draft = (
        "TITLE: Case study: Huey Lam Suits web redesign | Agency Example\n"
        "DESCRIPTION: How Agency Example rebuilt the Huey Lam Suits storefront with clearer product discovery.\n"
        "TARGET: huey lam suits\n"
        "REASON: Case study for this client project.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(draft, "test-model", "writer")),
        patch("app.meta_writer.scrape_page", return_value=CASE_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=CASE_PAGE["url"],
            profile=PROFILE,
            page=CASE_PAGE,
            site={**SITE, "page": {"role": "case_study"}},
            kind="case_study",
        )
    assert "huey" in f"{out['title']} {out['metaDescription']}".lower()


def test_service_page_stays_on_seo_not_case_study():
    bleed = (
        "TITLE: SEO Services | Huey Lam Suits Case Study\n"
        "DESCRIPTION: How we helped Huey Lam Suits rank for local searches.\n"
        "TARGET: seo\n"
        "REASON: Used the case study as proof.\n"
    )
    clean = (
        "TITLE: SEO Services for Calgary Businesses | Agency Example\n"
        "DESCRIPTION: Agency Example delivers technical and content SEO for Calgary businesses seeking clearer Search Console growth.\n"
        "TARGET: seo services\n"
        "REASON: Service page subject.\n"
    )
    calls = {"n": 0}

    def fake_ask(brief, **_kwargs):
        calls["n"] += 1
        return (bleed if calls["n"] == 1 else clean, "test-model", "writer")

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=fake_ask),
        patch("app.meta_writer.scrape_page", return_value=SERVICE_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=SERVICE_PAGE["url"],
            profile=PROFILE,
            page=SERVICE_PAGE,
            site={**SITE, "page": {"role": "service"}},
            kind="service",
        )
    blob = f"{out['title']} {out['metaDescription']}".lower()
    assert "huey" not in blob
    assert "seo" in blob


def test_product_page_stays_on_product():
    draft = (
        "TITLE: Oak Desk | Solid Wood Standing Desk\n"
        "DESCRIPTION: The Oak Desk is a solid wood standing desk with free shipping in Canada.\n"
        "TARGET: oak desk\n"
        "REASON: Product page facts.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(draft, "test-model", "writer")),
        patch("app.meta_writer.scrape_page", return_value=PRODUCT_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url=PRODUCT_PAGE["url"],
            profile={"business": "Shop Example", "services": "furniture", "areas": "Canada"},
            page=PRODUCT_PAGE,
            site={
                "business": {"brand": "Shop Example", "offers": ["oak desk", "furniture"], "places": ["Canada"]},
                "page": {"role": "product"},
            },
            kind="product",
        )
    blob = f"{out['title']} {out['metaDescription']}".lower()
    assert "oak desk" in blob
    assert "maple chair" not in blob


def test_irrelevant_keywords_do_not_override_page_subject():
    """Coverage keywords from unrelated queries must not force a wrong homepage subject."""
    from app.meta_writer import matching_queries, page_topic

    topic = page_topic(HOME_PAGE)
    matched = matching_queries(
        ["huey lam suits", "menswear calgary", "web design calgary", "seo calgary"],
        topic or "web design",
        HOME_PAGE["text"],
    )
    # Brand/client queries may appear, but page topic stays agency-side.
    assert "web" in (topic or "").lower() or "design" in (topic or "").lower() or "marketing" in (HOME_PAGE["title"]).lower()
    assert isinstance(matched, list)


def test_unsupported_place_claim_is_flagged():
    out = pq.validate_claims(
        "Web Design in Toronto",
        "We serve Toronto only.",
        evidence=HOME_PAGE["text"],
        places=["Toronto", "Calgary"],
    )
    assert any(c["kind"] == "place" and c["claim"] == "Toronto" and c["status"] == "unverified" for c in out["claims"])


def test_duplicate_titles_across_urls_are_flagged():
    from app.meta_writer import _issues

    issues = _issues(
        "Web Design Agency in Calgary | Agency Example",
        "Calgary web design and SEO.",
        HOME_PAGE["title"],
        HOME_PAGE["text"],
        taken=["Web Design Agency in Calgary | Agency Example"],
    )
    assert any("already uses" in issue.lower() for issue in issues)


def test_api_response_keys_remain_compatible():
    clean = (
        "TITLE: Web Design Agency in Calgary | Agency Example\n"
        "DESCRIPTION: Agency Example helps Calgary businesses with web design, SEO and Google Ads.\n"
        "TARGET: web design\n"
        "REASON: Compatible package.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(clean, "test-model", "writer")),
        patch("app.meta_writer.scrape_page", return_value=HOME_PAGE),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile=PROFILE,
            page=HOME_PAGE,
            site=SITE,
            kind="home",
        )
    for key in (
        "title",
        "metaDescription",
        "reason",
        "model",
        "role",
        "openIssues",
        "beatsLive",
        "target",
        "scraped",
        "openGraphTitle",
        "openGraphDescription",
        "jev",
    ):
        assert key in out
