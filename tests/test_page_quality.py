import httpx
import pytest

from app import page_quality as pq
from app import site_scan


def test_page_types():
    assert pq.page_type("https://a.com/") == "home"
    assert pq.page_type("https://a.com/case-studies/dental-clinic-dhaka") == "case_study"
    assert pq.page_type("https://a.com/portfolio/acme") == "case_study"
    assert pq.page_type("https://a.com/blog/2024/05/tips") == "blog"
    assert pq.page_type("https://a.com/privacy-policy") == "legal"
    assert pq.page_type("https://a.com/plumbing", role="service") == "service"


@pytest.mark.parametrize(
    "page, status",
    [
        (None, "failed"),
        ({"error": "HTTP 404"}, "failed"),
        ({"noindex": True, "words": 900}, "noindex"),
        ({"canonicalElsewhere": True, "canonical": "https://a.com/x", "words": 900}, "non_canonical"),
        ({"words": 40}, "thin"),
        ({"words": 600}, "ok"),
    ],
)
def test_content_status(page, status):
    assert pq.content_status(page)[0] == status


def test_claims_verified_partial_unverified_contradicted():
    page = "We have served Calgary for 12 years. Free estimate on every job. Licensed and insured."
    assert pq.validate_claims("Calgary Plumbing, 12 Years", "Free estimate today.", evidence=page, places=["Calgary"])["status"] == "verified"
    assert pq.validate_claims("Calgary Plumbing, 12 Years", "24/7 emergency service.", evidence=page)["status"] == "partially_verified"
    assert pq.validate_claims("Award-winning plumbers", "500 clients served.", evidence=page)["status"] == "unverified"
    result = pq.validate_claims("Calgary Plumbing, 20 Years", "Free estimate.", evidence=page)
    assert result["status"] == "contradicted"
    assert any(c["status"] == "contradicted" and c["page"].startswith("12") for c in result["claims"])
    assert pq.validate_claims("Plumbing repairs in Calgary", "Book a visit.", evidence=page)["status"] == "verified"


def test_place_not_on_page_is_unverified():
    out = pq.validate_claims("Dentist in Toronto", "Book today.", evidence="Our clinic in Calgary.", places=["Toronto", "Calgary"])
    assert out["status"] == "unverified"


def test_case_study_owner_versus_client():
    bad = pq.case_study_problems("Bright Smiles Dental Clinic Dhaka", "We are the best dental clinic in Dhaka.", {}, brand="NextCreavo")
    assert bad
    good = pq.case_study_problems("Case study: How NextCreavo helped Bright Smiles Dental", "Results of a site rebuild for a Dhaka clinic.", {}, brand="NextCreavo")
    assert good == []


def test_confidence_is_separate_from_validation():
    high = pq.confidence(content="ok", gsc={"impressions": 1200}, rivals=[{}], validation="unverified")
    low = pq.confidence(content="ok", gsc={"impressions": "—"}, rivals=[], validation="verified")
    assert high["level"] == "high" and low["level"] == "medium"


HTML = """<html><head><title>Plumbing | Acme</title>
<meta property="og:description" content="OG text">
<meta name="description" content="Real meta description">
<link rel="canonical" href="https://acme.example/plumbing/"></head>
<body><h1>Plumbing</h1><p>{body}</p></body></html>"""


def _client(routes):
    def handler(request):
        url = str(request.url)
        if url in routes:
            status, body = routes[url]
            return httpx.Response(status, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="nope", headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_read_page_prefers_meta_description_and_records_facts():
    with _client({"https://acme.example/plumbing": (200, HTML.format(body="word " * 200))}) as client:
        page = site_scan.read_page(client, "https://acme.example/plumbing")
    assert page["description"] == "Real meta description"
    assert page["ogDescription"] == "OG text"
    assert page["status"] == 200 and page["contentHash"] and page["fetchedAt"]
    assert page["canonicalElsewhere"] is False


def test_read_page_records_http_errors_and_canonical_elsewhere():
    other = HTML.replace("https://acme.example/plumbing/", "https://acme.example/services/")
    with _client({"https://acme.example/a": (200, other.format(body="x")), "https://acme.example/gone": (410, "")}) as client:
        assert site_scan.read_page(client, "https://acme.example/a")["canonicalElsewhere"] is True
        gone = site_scan.read_page(client, "https://acme.example/gone")
    assert gone["error"] == "HTTP 410" and gone["status"] == 410


def test_page_key_dedupes_variants():
    keys = {site_scan.page_key(u) for u in ("https://www.acme.com/about/", "http://acme.com/about", "https://acme.com/about")}
    assert len(keys) == 1
    assert site_scan.page_key("https://acme.com") == site_scan.page_key("https://acme.com/")
