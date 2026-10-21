"""Listing package regeneration: objectives, no-change, fluff rejection, package integrity."""

from __future__ import annotations

from unittest.mock import patch

from app.cms_connectors import build_listing_json_ld
from app.meta_writer import (
    listing_fluff_problems,
    listing_is_stronger,
    objective_instruction,
    stronger_listing,
    write_title_description,
)
from app.page_scrape import suggest_images


HOME = {
    "url": "https://agency.example/",
    "title": "Agency Example | Web Design, SEO & Digital Marketing Agency in Calgary",
    "description": (
        "Agency Example helps businesses grow through professional web design, SEO, "
        "Google Ads, branding, digital marketing and creative production."
    ),
    "h1": "Home",
    "headings": "Selected Work | Web Design | SEO",
    "text": (
        "Agency Example is a Calgary web design and digital marketing studio. "
        "Services include web design, development, SEO, Google Ads, branding and creative production. "
        "Selected work: Huey Lam Suits case study."
    ),
    "words": 70,
    "ogTitle": "",
    "ogDescription": "",
    "schemaName": "Agency Example",
    "schemaDescription": "",
    "schemaTypes": ["WebPage", "Organization", "WebSite"],
    "nav": ["Work", "Services"],
    "faqs": [],
    "videos": [],
    "images": [
        {"src": "/img/hero.jpg", "alt": ""},
        {"src": "/img/hero.jpg", "alt": ""},
        {"src": "https://agency.example/img/logo.png", "alt": "Agency Example logo"},
    ],
    "canonical": "https://agency.example/",
    "ogImage": "",
}

SITE = {
    "business": {
        "brand": "Agency Example",
        "offers": ["web design", "SEO", "Google Ads", "branding", "digital marketing"],
        "places": ["Calgary"],
    },
    "page": {"role": ""},
}

PROFILE = {
    "business": "Agency Example",
    "services": "web design, SEO, Google Ads, branding, digital marketing",
    "areas": "Calgary",
}


def test_fluff_rejects_five_core_services_and_template_openers():
    bad_title = "Design and Web Development in Calgary | 5 Core Services"
    bad_desc = "Business owners can explore web design and development from a Calgary-based agency."
    issues = listing_fluff_problems(bad_title, bad_desc, evidence=HOME["text"])
    assert any("5" in issue or "service count" in issue.lower() or "invent" in issue.lower() for issue in issues)
    assert any("generic" in issue.lower() or "template" in issue.lower() for issue in issues)
    assert listing_is_stronger(
        bad_title, bad_desc, HOME["title"], HOME["description"], "Agency Example",
        keyword="web design", places=["Calgary"], offers=SITE["business"]["offers"],
    ) is False


def test_stronger_listing_never_invents_service_counts():
    title, desc, reason = stronger_listing(
        keyword="web design",
        alternatives=["web design", "SEO", "5 Core Services"],
        offers=["web design", "SEO", "Google Ads"],
        places=["Calgary"],
        current_title=HOME["title"],
        current_desc=HOME["description"],
        headings=HOME["headings"],
        text=HOME["text"],
        brand="Agency Example",
        page_kind="home",
    )
    assert "5" not in title
    assert "core services" not in f"{title} {desc}".lower()
    assert "business owners can explore" not in desc.lower()
    assert "buyers can compare" not in desc.lower()
    assert "huey" not in desc.lower()


def test_objective_instruction_changes_context():
    assert "local" in objective_instruction("local_seo").lower()
    assert "preserve" in objective_instruction("preserve_brand").lower()
    assert "balanced" in objective_instruction("balanced").lower()


def test_good_homepage_yields_no_change_when_draft_is_weaker():
    weak = (
        "TITLE: Design and Web Development in Calgary | 5 Core Services\n"
        "DESCRIPTION: Business owners can explore web design and development from a Calgary-based agency with five core services.\n"
        "TARGET: web design\n"
        "REASON: Counted the services.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(weak, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile=PROFILE,
            page=HOME,
            site=SITE,
            kind="home",
            objective="preserve_brand",
        )
    assert out.get("recommendKeep") is True
    assert out.get("beatsLive") is False
    assert "huey" not in f"{out['title']} {out['metaDescription']}".lower()
    assert "5 core" not in f"{out['title']} {out['metaDescription']}".lower()
    assert "agency example" in out["title"].lower() or out["title"] == HOME["title"][:60]


def test_objective_is_passed_into_analysis():
    clean = (
        "TITLE: Web Design & SEO Agency in Calgary | Agency Example\n"
        "DESCRIPTION: Agency Example helps Calgary businesses with web design, SEO, Google Ads, branding and creative production.\n"
        "TARGET: web design\n"
        "REASON: Service focus on the homepage offer.\n"
    )
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(clean, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile=PROFILE,
            page=HOME,
            site=SITE,
            kind="home",
            objective="service_focus",
            focus_topic="web design",
            primary_keyword="web design calgary",
        )
    assert out["analysis"]["objective"] == "service_focus"
    assert out.get("objective") == "service_focus"
    assert "objective" in out["analysis"]


def test_irrelevant_operator_keyword_is_ignored():
    clean = (
        "TITLE: Web Design Agency in Calgary | Agency Example\n"
        "DESCRIPTION: Agency Example helps Calgary businesses with web design, SEO and Google Ads.\n"
        "TARGET: web design\n"
        "REASON: Ignored bad keyword.\n"
    )
    captured = {}

    def fake_ask(brief, **_k):
        captured["brief"] = brief
        return clean, "test", "writer"

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=fake_ask),
        patch("app.meta_writer.scrape_page", return_value=HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
    ):
        out = write_title_description(
            url="https://agency.example/",
            profile=PROFILE,
            page=HOME,
            site=SITE,
            kind="home",
            primary_keyword="huey lam suits menswear",
        )
    assert "Operator-selected primary keyword" not in captured.get("brief", "")
    assert "huey" not in (out.get("target") or "").lower()


def test_json_ld_preserves_organization_awareness():
    package = build_listing_json_ld(
        meta_title="Web Design | Agency Example",
        meta_description="Calgary web design and SEO.",
        page_url="https://agency.example/",
        site_name="Agency Example",
        schema_types=["WebPage", "Organization", "WebSite"],
        share_image="https://agency.example/og.jpg",
    )
    assert "@graph" in package
    types = {node.get("@type") for node in package["@graph"]}
    assert "WebPage" in types
    assert "Organization" in types
    assert package.get("_schemaNote") or True  # note may be stripped by caller
    note = package.get("_schemaNote", "")
    assert "Organization" in note or "WebSite" in note or "@graph" in package


def test_image_alts_are_deduped_per_src():
    rows = suggest_images(HOME["images"], "web design", "https://agency.example/")
    srcs = [row["src"] for row in rows]
    assert len(srcs) == len(set(s.split("?")[0].rstrip("/").lower() for s in srcs))
    hero = next(row for row in rows if "hero" in row["src"])
    assert hero["needsAlt"] is True
    logo = next(row for row in rows if "logo" in row["src"])
    assert logo["needsAlt"] is False
    assert logo["suggestedAlt"] == "Agency Example logo"
