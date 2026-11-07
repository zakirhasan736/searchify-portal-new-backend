"""Trial report page health comes from the site scan; sitemap files are listed for the scan view."""

from app.routers.billing import _scan_health_counts


def test_pages_are_graded_by_their_worst_scan_finding():
    scan = {"pages": [
        {"url": "https://a.test/", "title": "", "description": "", "words": 400},  # no title: critical
        {"url": "https://a.test/b", "title": "A good long title for page b", "description": "", "words": 400},  # no description: warning
        {"url": "https://a.test/c", "title": "A good long title for page c", "words": 400,
         "description": "A description that is long enough to pass the length check for this page, honestly."},
        {"url": "https://a.test/broken", "error": "HTTP 500"},
    ]}
    counts = _scan_health_counts(scan)
    assert counts == {"red": 1, "yellow": 1, "green": 1}


def test_stored_counts_still_win():
    assert _scan_health_counts({"counts": {"red": 2, "yellow": 3, "green": 4}}) == {"red": 2, "yellow": 3, "green": 4}


def test_sitemap_index_lists_its_files():
    import httpx

    from app import audit_checks

    index = b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' \
            b'<sitemap><loc>https://a.test/page-sitemap.xml</loc></sitemap><sitemap><loc>https://a.test/post-sitemap.xml</loc></sitemap></sitemapindex>'
    pages = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.test/</loc></url><url><loc>https://a.test/b</loc></url></urlset>'
    posts = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.test/blog/x</loc></url></urlset>'

    def handler(request):
        body = {"/sitemap_index.xml": index, "/page-sitemap.xml": pages, "/post-sitemap.xml": posts}.get(request.url.path)
        return httpx.Response(200, content=body, headers={"content-type": "application/xml"}) if body else httpx.Response(200, text="<html></html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = audit_checks._check_sitemap(client, ["https://a.test/sitemap_index.xml"], "a.test")
    assert result["index"] is True and result["urlCount"] == 3
    assert result["files"] == [{"url": "https://a.test/page-sitemap.xml", "urls": 2}, {"url": "https://a.test/post-sitemap.xml", "urls": 1}]
