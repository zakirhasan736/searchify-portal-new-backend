"""Public free-scan market detection. No network."""

from app.site_scan import (
    _detect_city,
    _detect_market,
    _google_health,
    _rivals_from_rows,
    _schema_category,
    _seed_keywords,
    _usable_category,
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


def test_website_schema_is_not_treated_as_the_business():
    assert _usable_category("Web Site") == ""
    assert _usable_category("WebSite") == ""
    assert _usable_category("FAQPage") == ""
    assert _usable_category("FAQ Page") == ""
    assert _usable_category("Reifenpreisvergleich") == "Reifenpreisvergleich"
    assert _schema_category([_page(schemaTypes=["FAQPage", "WebSite", "Product"])]) == ""


def test_rival_seeds_use_the_tire_keywords_not_web_site():
    seeds = _seed_keywords(
        [{"keyword": "sommerreifen", "volume": 22200}],
        [{"keyword": "reifen", "volume": 110000}],
        "Web Site",
    )
    assert seeds == ["reifen", "sommerreifen"]


def test_rival_seeds_skip_faqpage_and_keep_the_tire_category():
    seeds = _seed_keywords(
        [{"keyword": "sommerreifen", "volume": 22200}],
        [{"keyword": "reifen", "volume": 110000}, {"keyword": "FAQPage", "volume": 50000}],
        "FAQPage",
    )
    assert seeds == ["reifen", "sommerreifen"]
    typed = _seed_keywords(
        [{"keyword": "sommerreifen", "volume": 22200}],
        [{"keyword": "reifen", "volume": 110000}],
        "Reifenpreisvergleich",
    )
    assert typed == ["Reifenpreisvergleich", "reifen"]


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
