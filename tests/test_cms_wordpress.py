"""WordPress publish must resolve homepage `/` and nested permalinks to a post/page id."""

from app import cms_connectors


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, auth=None, params=None):
        self.calls.append((url, params or {}))
        key = url.rstrip("/")
        for pattern, responder in self.routes:
            if pattern in key:
                return responder(params or {})
        return FakeResponse(404, {})


def test_wp_resolve_homepage_uses_page_on_front():
    client = FakeClient([
        ("/wp/v2/settings", lambda _p: FakeResponse(200, {"show_on_front": "page", "page_on_front": 42})),
    ])
    post_id, resource = cms_connectors._wp_resolve_id(
        client,
        "https://sovereignstandard.ca/",
        ("user", "pass"),
        {"targetUrl": "/", "resource": "page"},
    )
    assert post_id == "42" and resource == "page"


def test_wp_resolve_homepage_matches_root_link_when_settings_missing():
    client = FakeClient([
        ("/wp/v2/settings", lambda _p: FakeResponse(403, {})),
        ("/wp/v2/pages", lambda p: FakeResponse(200, [
            {"id": 7, "link": "https://www.sovereignstandard.ca/", "slug": "home"},
            {"id": 9, "link": "https://sovereignstandard.ca/about/", "slug": "about"},
        ]) if not p.get("slug") else FakeResponse(200, [])),
    ])
    post_id, resource = cms_connectors._wp_resolve_id(
        client,
        "https://sovereignstandard.ca/",
        ("user", "pass"),
        {"targetUrl": "https://sovereignstandard.ca/", "resource": "page"},
    )
    assert post_id == "7" and resource == "page"


def test_wp_resolve_slug_still_works():
    client = FakeClient([
        ("/wp/v2/pages", lambda p: FakeResponse(200, [{"id": 11, "link": "https://acme.ca/services/", "slug": "services"}])
         if p.get("slug") == "services" else FakeResponse(200, [])),
        ("/wp/v2/posts", lambda _p: FakeResponse(200, [])),
    ])
    post_id, resource = cms_connectors._wp_resolve_id(
        client,
        "https://acme.ca/",
        ("user", "pass"),
        {"targetUrl": "https://acme.ca/services/", "resource": "page"},
    )
    assert post_id == "11" and resource == "page"


def test_norm_wp_url_strips_www_and_trailing_slash():
    assert cms_connectors._norm_wp_url("https://www.Example.com/About/") == "example.com/about"
    assert cms_connectors._norm_wp_url("https://example.com/") == "example.com/"
