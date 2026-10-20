from app import dfs_meta
from app.listing_research import best_keyword_from_research, idea_keywords, research_lines, visibility_gap


def test_pack_source_includes_keyword_and_page_without_inventing_ranks():
    text = dfs_meta.pack_source_text(
        keyword="web design and development",
        topic="portfolio",
        offers=["web design", "digital marketing"],
        current_title="Creative Studio",
        current_desc="Selected work.",
        page_text="We design and build websites for local brands.",
        rival_title="Studio North | Web Design and Development",
        anchors=["web design calgary"],
        visibility_gap="AI visibility gap: brand not named.",
    )
    assert "web design and development" in text
    assert "Studio North" in text
    assert "web design calgary" in text
    assert "invent" not in text.lower()


def test_generate_meta_tags_returns_empty_on_short_text():
    assert dfs_meta.generate_meta_tags("too short") == {}


def test_best_keyword_prefers_on_page_volume():
    stored = {
        "keywords": {
            "ranked": [
                {"keyword": "brand name", "url": "https://ex.com/about", "volume": 900, "position": 2},
                {"keyword": "web design calgary", "url": "https://ex.com/", "volume": 400, "position": 8},
            ],
            "ideas": [
                {"keyword": "custom website design", "volume": 1200},
                {"keyword": "plumbing repair", "volume": 5000},
            ],
        }
    }
    best, alts = best_keyword_from_research(
        stored,
        url="https://ex.com/",
        topic="web design",
        offers=["web design", "digital marketing"],
        queries=["web design"],
    )
    assert best.lower() == "web design calgary"
    assert any("custom website design" in item.lower() for item in alts)
    ideas = idea_keywords(stored, "web design", ["web design"])
    assert all("plumbing" not in row["keyword"].lower() for row in ideas)


def test_research_lines_include_ideas_backlinks_and_visibility():
    stored = {
        "keywords": {
            "ranked": [],
            "ideas": [{"keyword": "web design agency", "volume": 880}],
        },
        "backlinks": {
            "summary": {"referringDomains": 12, "rank": 40},
            "links": [{"target": "https://ex.com/work", "anchor": "portfolio web design"}],
        },
        "visibility": [
            {"engine": "ChatGPT", "mention": False, "status": "ok", "text": "who builds websites"},
        ],
    }
    lines = research_lines(stored, "https://ex.com/work", "web design", ["web design"])
    assert "web design agency" in lines
    assert "referring domains 12" in lines
    assert "portfolio web design" in lines
    assert "not mentioned" in lines
    assert "AI visibility gap" in visibility_gap(stored)
