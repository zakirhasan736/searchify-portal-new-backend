"""Public free-scan market detection. No network."""

from app.site_scan import (
    _detect_city,
    _detect_market,
    _google_health,
    _read_category_and_topics,
    _rivals_from_rows,
    _seed_keywords,
)


def _page(**extra):
    base = {
        "lang": "",
        "title": "",
        "description": "",
        "h1": "",
        "headings": [],
        "nav": [],
        "schemaName": "",
        "schemaTypes": [],
        "schemaCity": "",
        "text": "",
        "url": "https://example.com/",
    }
    base.update(extra)
    return base


def test_german_domain_is_germany_not_the_united_states():
    country, language, source = _detect_market(
        "https://www.reifexa.de/",
        [_page(lang="de-DE", text="Willkommen", url="https://www.reifexa.de/")],
    )
    assert (country, language, source) == ("Germany", "de", "tld")


def test_named_city_on_a_com_domain_uses_that_country():
    country, language, source = _detect_market(
        "https://example.com/",
        [_page(lang="en", text="We serve Calgary and Edmonton")],
    )
    assert (country, language, source) == ("Canada", "en", "page")


def test_german_city_comes_from_the_page_copy():
    assert _detect_city([_page(text="Unser Standort in Hamburg", url="https://www.reifexa.de/")], "Germany") == "Hamburg"


def test_schema_city_wins():
    assert _detect_city([_page(schemaCity="Leipzig", text="Hamburg")], "Germany") == "Leipzig"


def test_category_and_topics_come_from_the_ai_read():
    pages = [_page(schemaTypes=["FAQPage", "WebSite", "Product"], text="Sommerreifen und Winterreifen")]
    category, topics = _read_category_and_topics(
        {
            "category": "Reifenpreisvergleich",
            "topics": ["Sommerreifen", "Winterreifen"],
            "offers": ["Ganzjahresreifen", "FAQPage"],
        },
        pages,
    )
    assert category == "Reifenpreisvergleich"
    assert topics[:3] == ["Sommerreifen", "Winterreifen", "Ganzjahresreifen"]
    assert "FAQPage" not in topics


def test_schema_type_on_the_page_is_not_the_category():
    pages = [_page(schemaTypes=["FAQPage", "WebSite", "Product"])]
    category, topics = _read_category_and_topics(
        {"category": "FAQPage", "topics": ["WebSite"], "offers": ["Product"]},
        pages,
    )
    assert category == ""
    assert topics == []


def test_rival_seeds_use_the_read_not_a_schema_type():
    schema_keys = {"faqpage", "website", "product"}
    seeds = _seed_keywords(
        [{"keyword": "sommerreifen", "volume": 22200}],
        [{"keyword": "reifen", "volume": 110000}, {"keyword": "FAQPage", "volume": 50000}],
        "FAQPage",
        ["WebSite"],
        schema_keys,
    )
    assert seeds == ["reifen", "sommerreifen"]
    typed = _seed_keywords(
        [{"keyword": "sommerreifen", "volume": 22200}],
        [{"keyword": "reifen", "volume": 110000}],
        "Reifenpreisvergleich",
        ["Sommerreifen"],
        schema_keys,
    )
    assert typed == ["Reifenpreisvergleich", "Sommerreifen"]


def test_rival_bars_come_from_those_search_results():
    rows = _rivals_from_rows(
        "reifexa.de",
        {
            "reifen": [
                {"title": "Reifen.com", "url": "https://www.reifen.com/", "rank": 1},
                {"title": "Websites.ca", "url": "https://websites.ca/", "rank": 8},
            ],
            "FAQPage": [
                {"title": "Dribbble", "url": "https://dribbble.com/shots/faq", "rank": 2},
                {"title": "Elfsight", "url": "https://elfsight.com/faq-widget/", "rank": 3},
            ],
        },
        2,
    )
    labels = [row["label"] for row in rows]
    assert labels[0] == "reifen.com"
    assert "You · reifexa.de" in labels
    assert rows[0]["signal"] == "#1 for reifen"
    assert "dribbble.com" not in labels
    assert "elfsight.com" not in labels
    assert all("Edmonton" not in (row.get("address") or "") for row in rows)


def test_google_health_heatmap_uses_live_rankings():
    health = _google_health(
        page_rows=[{"color": "green", "path": "/", "label": "Listing is in place"}],
        ranked=[{"keyword": "sommerreifen", "position": 45, "volume": 22200}],
        onpage={"score": 97.1},
        pagespeed={"seo": "100", "performance": "28"},
    )
    assert health["kind"] == "rankings"
    assert health["dots"] == [
        {"color": "red", "path": "", "label": "sommerreifen", "position": 45},
    ]
    assert health["counts"]["red"] == 1
    assert health["grade"] in {"C", "C+", "D"}
    assert health["gradeNote"] == "0 of 1 searches on page one"
    assert "On-page 97.1" in health["gradeDetail"]
    assert "PageSpeed SEO 100" in health["gradeDetail"]
