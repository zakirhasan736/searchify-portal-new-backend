"""The whole-site concept drives every page; contact/about pages are about their purpose."""

from __future__ import annotations

from unittest.mock import patch

from app import meta_writer as M
from app.page_quality import page_type

SITE = {
    "business": {
        "brand": "Sovereign Standard",
        "business": "Sovereign Standard is a Calgary digital agency offering web development, UI/UX design, SEO, digital marketing, branding and Google Ads.",
        "category": "digital agency",
        "offers": ["web development", "UI/UX design", "SEO", "digital marketing", "branding", "Google Ads"],
        "places": ["Calgary"],
    },
    "page": {"role": ""},
}

CONTACT = {
    "url": "https://sovereignstandard.ca/contact-inquiry",
    "title": "Contact - Sovereign Standard",
    "description": "Contact Meeting of the Minds. You care about what you’re building. We do too. Calgary, Alberta / Working remotely LET’S TALK Tell us where...",
    "h1": "Meeting of the Minds",
    "headings": "Let's Talk | Tell us where you are",
    "text": (
        "Meeting of the Minds. You care about what you’re building. We do too. Calgary, Alberta / Working remotely. "
        "LET’S TALK. Tell us where you are and where you want to go. Send an inquiry with your project details."
    ),
    "words": 50, "schemaTypes": ["WebPage"], "images": [],
}

HOME = {
    "url": "https://sovereignstandard.ca/",
    "title": "Sovereign Standard | Web Development",
    "description": "Web development in Calgary.",
    "h1": "We Build Brands That Grow",
    "headings": "Web Development | UI/UX Design | SEO | Digital Marketing",
    "text": "Sovereign Standard is a Calgary digital agency: web development, UI/UX design, SEO, digital marketing, branding and Google Ads.",
    "words": 40, "schemaTypes": ["WebPage"], "images": [],
}


def _run(page, drafts, kind):
    seen, briefs = [], []

    def judge(evidence, listings, salt=""):
        seen.extend(row["title"] for row in listings)
        return [{**row, "scores": {}, "score": 35.0 if i == 0 else 82.0, "accurate": True, "note": "n"} for i, row in enumerate(listings)]

    def ask(brief, **_k):
        briefs.append(brief)
        return drafts, "t", "w"

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=ask),
        patch("app.meta_writer.scrape_page", return_value=page),
        patch("app.meta_writer.judge_listings", side_effect=judge),
    ):
        out = M.write_title_description(
            url=page["url"], profile={"business": "Sovereign Standard"}, page=page, site=SITE, kind=kind,
            queries=["web design calgary", "seo calgary"], content_seed={},
        )
    return out, seen, briefs[0]


def test_contact_page_is_about_contacting_the_whole_business():
    assert page_type(CONTACT["url"]) == "contact"
    drafts = (
        "TITLE: Design & Web Development: Let's Talk - Sovereign Standard\n"
        "DESCRIPTION: Send an inquiry to Sovereign Standard with your details. Based in Calgary and working remotely, the agency offers Google Ads management and branding.\n"
        "TITLE: Contact Our Calgary Digital Agency - Sovereign Standard\n"
        "DESCRIPTION: Tell Sovereign Standard about your project. A Calgary digital agency for web development, UI/UX, SEO and digital marketing, working remotely too.\n"
    )
    out, seen, brief = _run(CONTACT, drafts, "contact")
    assert out["target"] == "Contact" or out["analysis"]["keyword"] == "Contact"
    assert "Design & Web Development: Let's Talk - Sovereign Standard" not in seen  # a service ad on a contact page
    assert out["title"] == "Contact Our Calgary Digital Agency - Sovereign Standard"
    assert "Page purpose: Contact" in brief
    assert "Everything this site offers: web development, UI/UX design, SEO, digital marketing, branding, Google Ads" in brief
    assert "TITLE FORMAT: <page keyword and page-specific detail> - Sovereign Standard" in brief


def test_multi_service_homepage_leads_with_the_umbrella_category():
    drafts = (
        "TITLE: Sovereign Standard | Web Development & Design in Calgary\n"
        "DESCRIPTION: Sovereign Standard builds websites for Calgary businesses, from design to development, with clear project updates throughout.\n"
        "TITLE: Sovereign Standard | Calgary Digital Agency: Web, UX & SEO\n"
        "DESCRIPTION: A Calgary digital agency for web development, UI/UX design, SEO, digital marketing, branding and Google Ads, from strategy to launch.\n"
    )
    out, seen, brief = _run(HOME, drafts, "home")
    assert out["analysis"]["keyword"] == "digital agency"
    assert "Sovereign Standard | Web Development & Design in Calgary" not in seen  # narrows the business to one service
    assert out["title"] == "Sovereign Standard | Calgary Digital Agency: Web, UX & SEO"
    assert "Umbrella category: digital agency" in brief


def test_all_failed_drafts_keep_the_published_listing():
    drafts = "TITLE: Web Development Services - Sovereign Standard\nDESCRIPTION: Web development for Calgary businesses.\n"
    with patch("app.meta_writer.judge_listings", return_value=None):
        out, _seen, _brief = _run(CONTACT, drafts, "contact")
    assert out["title"] == CONTACT["title"]
    assert out["recommendKeep"] is True


def test_keyword_ideas_are_matched_to_the_business_and_its_pages(monkeypatch):
    from app import research

    concept = {
        "summary": SITE["business"]["business"],
        "category": "digital agency",
        "offers": SITE["business"]["offers"],
        "pages": [
            {"url": "https://sovereignstandard.ca/", "role": "home", "target": "calgary digital agency"},
            {"url": "https://sovereignstandard.ca/seo", "role": "service", "target": "seo services calgary"},
            {"url": "https://sovereignstandard.ca/ui-ux", "role": "service", "target": "ui ux design agency"},
            {"url": "https://sovereignstandard.ca/contact-inquiry", "role": "contact", "target": "contact sovereign standard"},
        ],
    }

    def fake_call(method, path, payload, timeout=60):
        if "keywords_for_site" in path:
            return [{"items": [
                {"keyword": "sovereign standard calgary", "keyword_info": {"search_volume": 50}},
                {"keyword": "seo services calgary", "keyword_info": {"search_volume": 700}},
                {"keyword": "free stock photos", "keyword_info": {"search_volume": 90000}},
                {"keyword": "ui ux design agency", "keyword_info": {"search_volume": 300}},
            ]}], 0.0
        return [{"items": []}], 0.0

    monkeypatch.setattr(research, "_call", fake_call)
    monkeypatch.setattr(research, "site_concept", lambda *a: concept)
    monkeypatch.setattr(research, "_place_for", lambda *a, **k: (research.locations.Location(country="Canada", iso="CA", language="en"), {}))
    monkeypatch.setattr(research, "business_offers", lambda *a: concept["offers"])
    monkeypatch.setattr(research, "cached", lambda *a, **k: None)
    for name in ("_budget", "_spend", "_save"):
        monkeypatch.setattr(research, name, lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "check", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "tracked_keyword_limit", lambda *a: None)
    monkeypatch.setattr(research.quotas, "keyword_idea_limit", lambda *a: 20)

    class U:
        id, role, plan = 1, "ROLE_CLIENT", "starter"

    out = research.keywords(None, U(), site="https://sovereignstandard.ca", terms=[])
    by_kw = {row["keyword"]: row for row in out["ideas"]}
    assert "free stock photos" not in by_kw  # not something this business sells
    assert by_kw["seo services calgary"]["page"] == "https://sovereignstandard.ca/seo"
    assert by_kw["ui ux design agency"]["page"] == "https://sovereignstandard.ca/ui-ux"
    assert by_kw["seo services calgary"]["concept"] == "SEO"
