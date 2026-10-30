from datetime import datetime, timedelta

from app import audit_checks as ac


def keys(issues):
    return [i["key"] for i in issues]


# --- crawl (DataForSEO) -------------------------------------------------------------------

def test_status_codes_grouped_with_linking_pages():
    items = [
        {"url": "https://a.com/old", "status_code": 404, "resource_type": "broken"},
        {"url": "https://a.com/gone", "status_code": 404},
        {"url": "https://a.com/api", "status_code": 503},
        {"url": "https://a.com/x", "status_code": 502},
        {"url": "https://a.com/ok", "status_code": 200},
        {"url": "https://a.com/tmp", "status_code": 302},
    ]
    broken = [{"from": "https://a.com/", "to": "https://a.com/old"}]
    issues = {i["key"]: i for i in ac.status_code_issues(items, broken)}
    assert set(issues) == {"status_404", "status_502", "status_503", "temporary_redirects"}
    assert issues["status_404"]["count"] == 2 and issues["status_404"]["severity"] == "Critical"
    assert "Linked from: https://a.com/" in issues["status_404"]["pages"][0]["detail"]
    assert issues["status_503"]["name"] == "Pages return 503 Service unavailable"
    assert any("maintenance" in s for s in issues["status_503"]["steps"])
    assert issues["status_404"]["helpUrl"].startswith("https://developers.google.com/")


def test_redirect_chains_loops_and_downgrades():
    chains = [
        {"is_redirect_loop": True, "chain": [{"link_from": "https://a.com/a", "link_to": "https://a.com/b"}, {"link_from": "https://a.com/b", "link_to": "https://a.com/a"}]},
        {"is_redirect_loop": False, "chain": [
            {"link_from": "https://a.com/1", "link_to": "https://a.com/2", "page_from_scheme": "https", "page_to_scheme": "http"},
            {"link_from": "https://a.com/2", "link_to": "https://a.com/3", "is_broken": True},
        ]},
    ]
    got = {i["key"]: i for i in ac.redirect_chain_issues(chains)}
    assert set(got) == {"redirect_loop", "redirect_to_broken", "redirect_chain_hops", "redirect_https_to_http"}
    assert "https://a.com/1 → https://a.com/2 → https://a.com/3" in got["redirect_chain_hops"]["pages"][0]["detail"]


def test_non_indexable_reasons():
    got = {i["key"]: i for i in ac.non_indexable_issues([{"url": "u1", "reason": "robots_txt"}, {"url": "u2", "reason": "meta_tag"}, {"url": "u3", "reason": "weird"}])}
    assert {"non_indexable_robots_txt", "non_indexable_meta_tag", "non_indexable_weird"} == set(got)


def test_canonical_checks():
    items = [
        {"url": "https://a.com/p1", "status_code": 200, "meta": {"canonical": ""}},
        {"url": "https://a.com/p2", "status_code": 200, "meta": {"canonical": "https://other.com/p2"}},
        {"url": "https://a.com/p3", "status_code": 200, "meta": {"canonical": "http://a.com/p3"}},
        {"url": "https://a.com/p4", "status_code": 200, "meta": {"canonical": "/p1"}},
        {"url": "https://a.com/p5", "status_code": 200, "meta": {"canonical": "https://a.com/p5/"}},
    ]
    assert keys(ac.canonical_issues(items, "a.com")) == ["canonical_other_domain", "canonical_http", "canonical_missing", "canonical_elsewhere"]


def test_spelling_skips_brands_and_finds_heading_typos():
    items = [{
        "url": "https://acme.com/",
        "meta": {
            "title": "Roof Repiar in Boston",
            "htags": {"h1": ["Fast roof repiar"]},
            "spell": {"misspelled": [{"word": "repiar"}, {"word": "Acme"}, {"word": "acme"}, {"word": "recieve"}, {"word": "ab"}, {"word": "hvac2"}]},
        },
    }]
    got = {i["key"]: i for i in ac.spelling_issues(items, {"acme"})}
    assert "“repiar”" in got["typo_heading"]["pages"][0]["detail"]
    assert got["typo_text"]["pages"][0]["detail"].endswith("“recieve”")


def test_heading_duplicates_and_sections():
    items = [
        {"url": "u1", "meta": {"htags": {"h1": ["Our services"]}, "content": {"plain_text_word_count": 900}}},
        {"url": "u2", "meta": {"htags": {"h1": ["Our services"], "h2": ["A"]}}},
        {"url": "u3", "meta": {"htags": {"h1": ["x" * 80], "h2": ["B"]}}},
    ]
    assert keys(ac.heading_duplicate_issues(items)) == ["duplicate_h1", "h1_too_long", "no_h2_sections"]


def test_broken_resources_css_is_critical():
    got = ac.broken_resource_issues(
        [{"url": "https://a.com/", "broken_resources": True}],
        [{"url": "https://a.com/style.css", "status_code": 404, "resource_type": "stylesheet"}, {"url": "https://a.com/ok.png", "status_code": 200}],
    )
    assert got[0]["severity"] == "Critical" and got[0]["pages"][0]["title"] == "CSS file" and got[0]["count"] == 2


# --- schema ---------------------------------------------------------------------------------

def _ld(obj):
    import json

    return [json.dumps(obj)]


def test_product_price_errors_low_higher_than_high_and_missing_currency():
    res = ac.validate_json_ld(_ld({
        "@context": "https://schema.org", "@type": "Product", "name": "Roof plan",
        "offers": {"@type": "AggregateOffer", "lowPrice": "900", "highPrice": "400"},
    }), "Plans from 400 to 900")
    msgs = " | ".join(e["message"] for e in res["errors"])
    assert "lowPrice (900) is higher than highPrice (400)" in msgs
    assert "missing priceCurrency" in msgs
    assert all(e["group"] == "price" for e in res["errors"])


def test_offer_price_format_expiry_and_visible_match():
    past = (datetime.utcnow() - timedelta(days=3)).date().isoformat()
    res = ac.validate_json_ld(_ld({
        "@context": "https://schema.org", "@type": "Product", "name": "Kit", "image": "x.jpg",
        "offers": {"@type": "Offer", "price": "$49.99", "priceCurrency": "usd", "priceValidUntil": past},
    }), "Kit costs 49.99")
    msgs = " | ".join(e["message"] for e in res["errors"])
    assert "not a plain number" in msgs and "3-letter ISO code" in msgs and "in the past" in msgs
    ok = ac.validate_json_ld(_ld({
        "@context": "https://schema.org", "@type": "Product", "name": "Kit", "image": "x.jpg",
        "offers": {"@type": "Offer", "price": "1200", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
    }), "Only $1,200 today")
    assert ok["errors"] == [] and ok["warnings"] == []
    hidden = ac.validate_json_ld(_ld({
        "@context": "https://schema.org", "@type": "Product", "name": "Kit", "image": "x.jpg",
        "offers": {"@type": "Offer", "price": "99", "priceCurrency": "USD", "availability": "InStock"},
    }), "Call us for a quote")
    assert "not shown on the page" in hidden["warnings"][0]["message"]


def test_reviews_ratings():
    res = ac.validate_json_ld(_ld({
        "@context": "https://schema.org", "@type": "LocalBusiness", "name": "Acme", "address": {"streetAddress": "1 Main", "addressLocality": "Boston", "postalCode": "02101", "addressCountry": "US"},
        "telephone": "1", "url": "u", "openingHours": "Mo-Fr",
        "aggregateRating": {"ratingValue": "6", "reviewCount": "0"},
        "review": [{"reviewRating": {"ratingValue": "5"}}],
    }))
    msgs = " | ".join(e["message"] for e in res["errors"])
    assert "outside 1–5" in msgs and "at least one review" in msgs and "missing author" in msgs


def test_faq_answers_must_be_visible_and_complete():
    res = ac.validate_json_ld(_ld({
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "Do you offer free quotes?", "acceptedAnswer": {"@type": "Answer", "text": "Yes, every quote is free of charge."}},
            {"@type": "Question", "name": "How fast?", "acceptedAnswer": {"@type": "Answer", "text": "Same day in most cases across the city"}},
            {"@type": "Question", "name": "", "acceptedAnswer": {}},
        ],
    }), "Do you offer free quotes? Yes, every quote is free of charge.")
    assert any("has no name" in e["message"] for e in res["errors"])
    assert any("has no acceptedAnswer.text" in e["message"] for e in res["errors"])
    assert res["warnings"][0]["message"].startswith("1 FAQ answer(s) in the schema are not visible")


def test_breadcrumb_graph_and_invalid_json():
    res = ac.validate_json_ld(['{"@context":"https://schema.org","@graph":[{"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","name":"Home"},{"@type":"ListItem","position":2,"name":"Roofs"}]}]}', "{not json"])
    assert res["invalid"] == 1
    msgs = [e["message"] for e in res["errors"]]
    assert any("not valid JSON" in m for m in msgs)
    assert any("item 1 is missing a numeric position" in m for m in msgs)
    assert any("item 1 (“Home”) is missing item" in m for m in msgs)
    assert "BreadcrumbList" in res["types"]


def test_video_and_image_objects():
    res = ac.validate_json_ld(_ld([{"@context": "https://schema.org", "@type": "VideoObject", "name": "Tour"}, {"@context": "https://schema.org", "@type": "ImageObject"}]))
    groups = {e["group"] for e in res["errors"]}
    assert groups == {"video", "image"}


# --- HTML analysis + scan issues ------------------------------------------------------------

HTML = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Plan","offers":{"@type":"AggregateOffer","lowPrice":"50","highPrice":"20","priceCurrency":"USD"}}</script>
<script>var x = "<h2>not a heading</h2>";</script>
</head><body>
<h2>Intro first</h2><h1>Roof repair</h1><h2>Services</h2><h4>Pricing</h4><h3></h3>
<div class="elementor-accordion"><details><summary>Is it free?</summary>Yes.</details><details><summary>How long?</summary>2 days.</details></div>
<img src="/uploads/IMG_1234.jpg"><img src="/a.png" width="10" height="10">
<p>Prices 20 to 50.</p>
</body></html>"""


def test_analyze_html_outline_schema_accordion_images():
    audit = ac.analyze_html(HTML, "https://a.com/")
    assert audit["outline"] == [[2, "Intro first"], [1, "Roof repair"], [2, "Services"], [4, "Pricing"]]
    assert audit["emptyHeadings"] == 1
    assert audit["accordion"] >= 2 and "Is it free?" in audit["questions"]
    assert audit["imagesNoSizeCount"] == 1 and audit["imageBadNames"] == ["/uploads/IMG_1234.jpg"]
    assert any("higher than highPrice" in e["message"] for e in audit["schema"]["errors"])


def test_scan_issues_from_pages_and_site_checks():
    scan = {
        "pages": [{"url": "https://a.com/", "title": "Roof", "audit": ac.analyze_html(HTML, "https://a.com/")}],
        "failed": [{"url": "https://a.com/missing", "status": 404}, {"url": "https://a.com/t", "status": None}],
        "siteChecks": {
            "robots": ac.check_robots("User-agent: *\nDisallow: /\nNoindex: /x\n", 200),
            "sitemap": {"problems": [{"key": "sitemap_bad_urls", "severity": "Warning", "detail": "d", "urls": [{"url": "https://a.com/r", "why": "redirects (301)"}]}]},
            "redirects": {"problems": [{"key": "http_no_redirect", "severity": "Warning", "detail": "http loads"}]},
        },
    }
    got = {i["key"]: i for i in ac.scan_issues(scan)}
    assert {"heading_sections", "empty_headings", "schema_price", "accordion_without_faq", "images_no_dimensions", "image_file_names",
            "scan_broken_404", "robots_blocks_site", "robots_noindex", "robots_no_sitemap", "sitemap_bad_urls", "http_no_redirect"} <= set(got)
    detail = got["heading_sections"]["pages"][0]["detail"]
    assert "before the H1" in detail and "H2 → H4 “Pricing” in section “Services”: H3 is skipped" in detail
    assert got["robots_blocks_site"]["severity"] == "Critical"
    assert got["sitemap_bad_urls"]["pages"][0] == {"url": "https://a.com/r", "status": None, "title": "", "description": "", "detail": "redirects (301)"}
    first = list(got.values())[0]
    assert first["severity"] == "Critical"


def test_robots_rules():
    r = ac.check_robots("User-agent: Googlebot\nDisallow: /wp-content/\nFoo bar\nCrawl-delay: 5\nSitemap: https://a.com/s.xml\n", 200)
    k = [p["key"] for p in r["problems"]]
    assert "robots_blocks_assets" in k and "robots_syntax" in k and "robots_crawl_delay" in k and "robots_no_sitemap" not in k
    assert r["sitemaps"] == ["https://a.com/s.xml"]
    assert ac.check_robots("", 503)["problems"][0]["key"] == "robots_5xx"
    assert ac.check_robots("", 404)["problems"][0]["key"] == "robots_missing"
    allow_all = ac.check_robots("User-agent: *\nDisallow: /\nAllow: /\n", 200)
    assert "robots_blocks_site" not in [p["key"] for p in allow_all["problems"]]


# --- Search Console URL Inspection ----------------------------------------------------------

def test_inspection_issues():
    results = [
        {"url": "https://a.com/", "result": {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}}},
        {"url": "https://a.com/old", "result": {"inspectionResult": {"indexStatusResult": {"verdict": "FAIL", "coverageState": "Not found (404)", "pageFetchState": "NOT_FOUND", "lastCrawlTime": "2026-10-01T00:00:00Z"}}}},
        {"url": "https://a.com/dup", "result": {"inspectionResult": {"indexStatusResult": {"verdict": "NEUTRAL", "coverageState": "Duplicate, Google chose different canonical than user",
                                                                                            "googleCanonical": "https://a.com/main", "userCanonical": "https://a.com/dup"}}}},
        {"url": "https://a.com/faq", "result": {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"},
                                                                     "richResultsResult": {"verdict": "FAIL", "detectedItems": [{"richResultType": "FAQ", "items": [{"name": "FAQ", "issues": [
                                                                         {"issueMessage": "Missing field \"acceptedAnswer\"", "severity": "ERROR"}, {"issueMessage": "Missing field \"url\"", "severity": "WARNING"}]}]}]}}}},
        {"url": "https://other.com/", "error": "URL Inspection failed (403)"},
    ]
    got = {i["key"]: i for i in ac.inspection_issues(results)}
    assert "gsc_search_console_page_not_found_404" in got
    assert "Last crawled 2026-10-01" in got["gsc_search_console_page_not_found_404"]["pages"][0]["detail"]
    assert "Google chose: https://a.com/main" in got["gsc_canonical_mismatch"]["pages"][0]["detail"]
    assert "acceptedAnswer" in got["gsc_rich_result_errors"]["pages"][0]["detail"]
    assert "gsc_rich_result_warnings" in got and "gsc_inspection_failed" in got
    assert list(got.values())[0]["severity"] == "Critical"


def test_audit_report_end_to_end_with_fake_provider(monkeypatch):
    from app import research

    html_page = {
        "url": "https://acme.com/", "resource_type": "html", "status_code": 200, "broken_resources": True,
        "checks": {"no_doctype": True},
        "meta": {"title": "Acme Roof Repiar", "canonical": "https://other.com/", "htags": {"h1": ["Roof repiar"]},
                 "spell": {"misspelled": [{"word": "repiar"}]}, "content": {"plain_text_word_count": 300}},
    }
    broken_page = {"url": "https://acme.com/old", "resource_type": "broken", "status_code": 404}

    def fake_call(method, path, payload=None, timeout=60.0):
        if path == "/on_page/pages":
            filters = (payload or [{}])[0].get("filters")
            return ([{"items": [broken_page] if filters else [html_page, broken_page]}], 0.0)
        if path == "/on_page/links":
            return ([{"items": [{"link_from": "https://acme.com/", "link_to": "https://acme.com/old", "page_to_status_code": 404}]}], 0.0)
        if path == "/on_page/redirect_chains":
            return ([{"items": [{"is_redirect_loop": True, "chain": [{"link_from": "https://acme.com/a", "link_to": "https://acme.com/b"}]}]}], 0.0)
        if path == "/on_page/non_indexable":
            return ([{"items": [{"url": "https://acme.com/private", "reason": "meta_tag"}]}], 0.0)
        if path == "/on_page/resources":
            return ([{"items": [{"url": "https://acme.com/s.css", "status_code": 404, "resource_type": "stylesheet"}]}], 0.0)
        raise research.ResearchError("unexpected " + path)

    monkeypatch.setattr(research, "_call", fake_call)
    summary = {"page_metrics": {"checks": {"is_4xx_code": 1}, "onpage_score": 71.5}, "crawl_status": {"pages_crawled": 2}, "domain_info": {"name": "acme.com", "checks": {}}}
    report = research._audit_report("task-1", summary)
    found = {i["key"] for i in report["issues"]}
    assert {"status_404", "redirect_loop", "non_indexable_meta_tag", "canonical_other_domain", "typo_heading", "broken_resources", "no_doctype", "broken_links"} <= found
    assert "is_4xx_code" not in found  # replaced by the exact 404 report
    assert report["reportVersion"] == research.AUDIT_REPORT_VERSION
    assert report["statusCounts"] == {"2xx": 1, "404": 1}
    assert report["issues"][0]["severity"] == "Critical"


def test_inspection_does_not_repeat_canonical_mismatch():
    got = ac.inspection_issues([{"url": "https://a.com/d", "result": {"inspectionResult": {"indexStatusResult": {
        "verdict": "NEUTRAL", "coverageState": "Duplicate, Google chose different canonical than user",
        "googleCanonical": "https://a.com/", "userCanonical": "https://a.com/d"}}}}])
    assert [i["key"] for i in got] == ["gsc_canonical_mismatch"]
