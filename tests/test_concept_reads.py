from app.concept_reads import backlink_concepts, keyword_concepts, offer_fit, visibility_reading
from app.research import _idea_seeds


def test_keyword_concepts_keep_the_offer_and_drop_unrelated_volume():
    payload = {
        "ideas": [
            {"keyword": "emergency plumber calgary", "volume": 120, "seed": "Calgary plumbing", "intent": "commercial"},
            {"keyword": "seo software pricing", "volume": 9000, "seed": "Calgary plumbing"},
        ],
        "ranked": [
            {"keyword": "calgary plumber", "volume": 500, "position": 3},
            {"keyword": "sovereign standard login", "volume": 40, "position": 1},
        ],
    }
    out = keyword_concepts(payload, ["Calgary plumbing"])
    titles = [row["title"] for row in out["rows"]]
    assert "emergency plumber calgary" in titles
    assert "calgary plumber" in titles
    assert "seo software pricing" not in titles
    assert "sovereign standard login" not in titles
    match = next(row for row in out["rows"] if row["title"] == "emergency plumber calgary")
    assert "Calgary plumbing" in match["why"]
    assert "not in the top 100" in match["why"]
    assert "120" in match["why"]
    ranking = next(row for row in out["rows"] if row["title"] == "calgary plumber")
    assert "position 3" in ranking["why"]


def test_keyword_concepts_without_a_site_read_stay_honest():
    out = keyword_concepts(
        {"ideas": [{"keyword": "emergency plumber calgary", "volume": 120, "seed": "calgary plumber"}]},
        [],
    )
    assert out["rows"][0]["title"] == "emergency plumber calgary"
    assert "Read the site" in out["rows"][0]["why"]
    assert "Read the site" in out["note"]


def test_keyword_concepts_say_when_stored_searches_miss_the_offer():
    out = keyword_concepts({"ideas": [{"keyword": "seo software pricing", "volume": 90, "seed": "seo"}]}, ["Calgary plumbing"])
    assert out["rows"] == []
    assert "Calgary plumbing" in out["note"]
    assert "Refresh live keyword data" in out["note"]


def test_idea_seeds_start_from_the_offer():
    seeds = _idea_seeds(["seo software"], [{"keyword": "login page"}], offers=["Calgary web design"])
    assert seeds[0] == "Calgary web design"
    assert "seo software" not in seeds


def test_visibility_reading_names_who_was_cited_instead():
    reading = visibility_reading(
        {
            "text": "Who offers web design in Calgary?",
            "status": "ok",
            "mention": False,
            "citation": "",
            "snippet": "Other studios include Northline.",
            "sources": [{"title": "Northline Studio", "url": "https://northline.example/"}],
        },
        ["web design"],
    )
    assert reading["concept"] == "web design"
    assert "does not name the business" in reading["reading"]
    assert "Northline Studio" in reading["reading"]
    assert "title" in reading["next"]


def test_visibility_reading_keeps_a_citation():
    reading = visibility_reading(
        {"text": "Who offers web design in Calgary?", "status": "ok", "mention": True, "citation": "https://acme.ca/", "snippet": "Acme."},
        ["web design"],
    )
    assert "links to the site" in reading["reading"]


def test_backlink_concepts_keep_anchors_that_match_the_offer():
    out = backlink_concepts(
        {"links": [
            {"domain": "news.com", "target": "https://acme.ca/design", "anchor": "Calgary web design", "state": "Active"},
            {"domain": "spam.example", "target": "https://acme.ca/", "anchor": "click here", "state": "Active"},
            {"domain": "old.example", "target": "https://acme.ca/design", "anchor": "web design studio", "state": "Lost"},
        ]},
        ["web design"],
    )
    titles = [row["title"] for row in out["rows"]]
    assert titles[0] == "news.com"
    assert "old.example" in titles
    assert "spam.example" not in titles
    assert "marked lost" in next(row["why"] for row in out["rows"] if row["title"] == "old.example")
    assert "does not build" in out["note"]


def test_backlink_concepts_say_when_no_anchor_matches():
    out = backlink_concepts(
        {"links": [{"domain": "directory.test", "target": "https://acme.ca/", "anchor": "website", "state": "Active"}]},
        ["roof repair"],
    )
    assert out["rows"][0]["title"] == "roof repair"
    assert "does not yet describe" in out["rows"][0]["why"]


def test_offer_fit_accepts_plumber_and_plumbing():
    score, offer = offer_fit("emergency plumber calgary", ["Calgary plumbing"])
    assert offer == "Calgary plumbing"
    assert score >= 0.34
