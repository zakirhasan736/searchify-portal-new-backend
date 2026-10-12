"""Listing signals and stored research selection. No network."""

from app.listing_research import keywords_for_page, ranked_on_url, research_lines
from app.meta_writer import page_topic
from app.page_scrape import PageParser, listing_from_parser
from app.site_scan import PageReader


HTML = """
<html><head>
<title>Old Browser Title</title>
<meta name="description" content="Old meta description for web design projects.">
<meta property="og:title" content="Old Social Title">
<meta property="og:description" content="Old social description.">
<meta property="og:type" content="website">
<link rel="canonical" href="https://example.com/projects">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Service","name":"Calgary Web Design","description":"Project work for Calgary businesses.","aggregateRating":{"@type":"AggregateRating","ratingValue":"4.9","reviewCount":"18"}}
</script>
</head><body>
<nav><a href="/seo">SEO</a><a href="/ads">Google Ads</a></nav>
<h1>Home</h1>
<h2>What is included?</h2>
<p>Calgary web design project pages for local businesses.</p>
<iframe src="https://www.youtube.com/embed/abc123"></iframe>
<img src="/photos/summit-roof.jpg" alt="">
</body></html>
"""


def test_parser_reads_open_graph_and_json_ld():
    parser = PageParser()
    parser.feed(HTML)
    listing = listing_from_parser(parser)
    assert listing["title"] == "Old Browser Title"
    assert listing["description"] == "Old meta description for web design projects."
    assert listing["ogTitle"] == "Old Social Title"
    assert listing["ogDescription"] == "Old social description."
    assert listing["schemaTypes"] == ["Service"]
    assert listing["schemaName"] == "Calgary Web Design"
    assert listing["canonical"] == "https://example.com/projects"
    assert listing["nav"] == ["SEO", "Google Ads"]
    assert listing["faqs"] == ["What is included?"]
    assert listing["videos"] == ["https://www.youtube.com/embed/abc123"]
    assert listing["reviewRating"] == "4.9"
    assert listing["reviewCount"] == "18"
    assert listing["images"][0]["src"] == "/photos/summit-roof.jpg"
    assert "Home" == listing["h1"]


def test_site_reader_keeps_schema_out_of_visible_copy():
    reader = PageReader("https://example.com/")
    reader.feed(HTML)
    assert "schema.org" not in " ".join(reader.text)
    assert reader.og_title == "Old Social Title"
    assert "Calgary Web Design" in "".join(reader.json_ld)


def test_weak_h1_does_not_become_the_topic():
    topic = page_topic({
        "h1": "Home",
        "schemaName": "Calgary Web Design",
        "ogTitle": "Old Social Title",
        "title": "Old Browser Title",
    })
    assert topic == "Calgary Web Design"


def test_keyword_prefers_the_url_searchify_already_ranks():
    ranked = [
        {"keyword": "google ads agency", "url": "https://example.com/ads", "volume": 900, "position": 4},
        {"keyword": "calgary web design", "url": "https://example.com/projects", "volume": 240, "position": 11},
    ]
    hits = ranked_on_url(ranked, "https://www.example.com/projects/")
    assert [row["keyword"] for row in hits] == ["calgary web design"]
    topical = keywords_for_page(ranked, "https://example.com/projects", "Calgary Web Design Projects")
    assert topical[0]["keyword"] == "calgary web design"


def test_image_text_uses_the_page_topic_when_alt_is_empty():
    from app.page_scrape import suggest_images

    rows = suggest_images(
        [
            {"src": "/photos/summit-roof.jpg", "alt": ""},
            {"src": "https://cdn.example.com/logo.png", "alt": "Sovereign Standard logo"},
        ],
        "Calgary web design projects",
        "https://sovereignstandard.ca/portfolios/",
    )
    assert rows[0]["needsAlt"] is True
    assert rows[0]["src"].startswith("https://sovereignstandard.ca/photos/")
    assert "Calgary web design projects" in rows[0]["suggestedAlt"]
    assert rows[1]["needsAlt"] is False
    assert rows[1]["suggestedAlt"] == "Sovereign Standard logo"
    text = research_lines(
        {
            "keywords": {"ranked": [{"keyword": "calgary web design", "url": "https://example.com/projects", "volume": 240, "position": 11}]},
            "backlinks": {"summary": {"referringDomains": 12, "rank": 40}, "links": [{"target": "https://example.com/projects", "anchor": "web design projects"}]},
            "visibility": [{"status": "ok", "engine": "ChatGPT", "mention": True, "snippet": "They design sites in Calgary."}],
        },
        "https://example.com/projects",
        "Calgary Web Design",
    )
    assert "calgary web design" in text
    assert "referring domains 12" in text
    assert "ChatGPT: brand mentioned" in text
