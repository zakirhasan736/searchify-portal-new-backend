from app.meta_writer import (
    brand_only_keyword,
    conceptual_rivals,
    coverage_keywords,
    drops_homepage_promise,
    listing_is_stronger,
    meets_competitor,
    rival_to_meet,
    stronger_listing,
)

OFFERS = ["digital marketing agency", "web design", "google ads"]
PLACES = ["Calgary"]
CURRENT = "Calgary Digital Marketing Agency | Selected Client Work - Sovereign Standard"
CURRENT_DESC = (
    "For Calgary business owners serious about growth: explore selected work in web development, "
    "SEO, Google Ads, branding and creative commercials."
)
NARROW = "Sovereign Creative Commercials | Selected Client Work"


def test_brand_search_is_not_the_homepage_keyword():
    assert brand_only_keyword("sovereign", "Sovereign Standard", "https://sovereignstandard.ca/")


def test_best_keyword_comes_from_the_business_not_the_city_in_the_current_title():
    options = coverage_keywords(
        offers=OFFERS,
        places=PLACES,
        queries=["sovereign", "calgary digital marketing agency", "web design calgary"],
        brand="Sovereign Standard",
        url="https://sovereignstandard.ca/",
    )
    assert options[0].lower() == "digital marketing agency"
    assert not options[0].lower().startswith("calgary")
    assert "sovereign" not in {item.lower() for item in options}
    assert any("web design" in item.lower() for item in options[1:])


def test_competitors_must_sell_the_same_kind_of_service():
    rivals = conceptual_rivals(
        [
            {"title": "Summit Roofers | Calgary roof repair", "h1": "Roof repair", "description": "Shingles and solar."},
            {"title": "Northline | Digital marketing agency", "h1": "Web design and Google Ads", "description": "Campaigns for local firms."},
        ],
        OFFERS,
    )
    assert [row["title"] for row in rivals] == ["Northline | Digital marketing agency"]


def test_williams_sonoma_is_not_a_web_design_competitor():
    """A nearby Places store must not become the title the draft is written to meet."""
    places = [
        {
            "title": "Cooking Classes & Kitchen Store | Littleton, CO | Aspen Grove | Williams Sonoma",
            "h1": "Cooking Classes",
            "description": "Cookware and kitchen store.",
            "source": "Google Places",
        },
        {
            "title": "Studio North | Web Design and Development",
            "h1": "Web design",
            "description": "Custom websites for agencies.",
            "source": "Google results",
        },
    ]
    kept = conceptual_rivals(places, OFFERS, keyword="Web design and development")
    assert all("Williams" not in row["title"] for row in kept)
    meet = rival_to_meet(kept or places, "Web design and development", OFFERS)
    assert "Williams" not in (meet.get("title") or "")
    assert "Web Design" in (meet.get("title") or "")
    empty = rival_to_meet(
        [places[0]],
        "Web design and development",
        OFFERS,
    )
    assert empty == {}


def test_near_copy_of_the_live_title_is_not_stronger():
    assert listing_is_stronger(
        "Calgary Digital Marketing Agency | Selected Client Work",
        CURRENT_DESC,
        CURRENT,
        CURRENT_DESC,
        "Sovereign Standard",
        keyword="digital marketing agency",
        places=PLACES,
        offers=OFFERS,
    ) is False


def test_stronger_listing_leads_with_the_business_keyword():
    title, desc, reason = stronger_listing(
        keyword="digital marketing agency",
        alternatives=["digital marketing agency", "web design", "google ads"],
        offers=OFFERS,
        places=PLACES,
        current_title=CURRENT,
        current_desc=CURRENT_DESC,
        headings="CREATIVE COMMERCIALS | WEB DEV",
        text="Selected work includes the RECTANGLE project and Summit Roofers.",
        brand="Sovereign Standard",
    )
    assert title.lower().startswith("digital marketing agency")
    assert not title.lower().startswith("calgary")
    assert "web design" in desc.lower() or "google ads" in desc.lower()
    assert "5 core" not in f"{title} {desc}".lower()
    assert "business owners can explore" not in desc.lower()
    assert "fallback" in reason.lower() or "lead" in reason.lower() or "keyword" in reason.lower()
    copied = listing_is_stronger(
        "Calgary Digital Marketing Agency | Web Design",
        "Calgary business owners can review web design.",
        CURRENT, CURRENT_DESC, "Sovereign Standard",
        keyword="digital marketing agency", places=PLACES, offers=OFFERS,
    )
    assert copied is False


def test_homepage_draft_that_drops_the_agency_and_city_is_rejected():
    assert drops_homepage_promise(NARROW, CURRENT, "Sovereign Standard")


def test_suggestion_must_meet_the_hard_competitor_on_the_same_keyword():
    rivals = [
        {"title": "Summit Roofers | Calgary roof repair", "source": "Google results"},
        {"title": "Northline Digital Marketing Agency | Web Design and Google Ads", "source": "Competitor you named"},
    ]
    meet = rival_to_meet(conceptual_rivals(rivals, OFFERS), "digital marketing agency", OFFERS)
    assert "Northline" in meet["title"]
    assert meets_competitor("Digital Marketing Agency | Web Design", meet, "digital marketing agency")
    assert not meets_competitor("Sovereign Creative Commercials | Selected Client Work", meet, "digital marketing agency")
    assert not meets_competitor(meet["title"], meet, "digital marketing agency")


def test_listing_package_keeps_open_graph_and_json_ld_on_the_same_words():
    from app.approval import sync_listing_package

    proposed = sync_listing_package(
        {
            "title": "Digital Marketing Agency | Web Design",
            "metaDescription": "Calgary buyers can compare web design and Google Ads. See the work.",
            "targetUrl": "https://sovereignstandard.ca/",
            "profileHints": {"business": "Sovereign Standard"},
        }
    )
    assert proposed["openGraphTitle"] == proposed["title"]
    assert proposed["openGraphDescription"] == proposed["metaDescription"]
    assert proposed["jsonLd"]["name"] == proposed["title"]
    assert proposed["jsonLd"]["description"] == proposed["metaDescription"]


def test_jev_scores_listing_packages_not_as_long_articles():
    from app.jev import score_listing

    strong = score_listing(
        title="Digital Marketing Agency | Web Design & Ads",
        description="Calgary buyers can compare web design, SEO, and Google Ads, including RECTANGLE. See the work.",
        keyword="digital marketing agency",
        live_title=CURRENT,
        live_description=CURRENT_DESC,
        beats_live=True,
        meet_competitor="Northline Digital Marketing Agency | Web Design and Google Ads",
        open_issues=[],
    )
    assert strong["score"] >= 55
    assert strong["band"] in {"auto", "inbox"}
    assert strong["checks"]["keyword_in_title"] is True
    assert strong["checks"]["beats_live"] is True

    paste = score_listing(
        title=CURRENT,
        description=CURRENT_DESC,
        keyword="digital marketing agency",
        live_title=CURRENT,
        live_description=CURRENT_DESC,
        beats_live=False,
        meet_competitor="",
        open_issues=["The draft is not stronger than the live title."],
    )
    assert paste["score"] < strong["score"]
    assert paste["checks"]["not_live_paste"] is False or paste["band"] in {"inbox", "reject"}
