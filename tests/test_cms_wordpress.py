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


def test_wp_meta_payload_sets_seo_keys_not_page_title():
    meta = cms_connectors._wp_meta_payload("Home meta title", "Home meta description")
    assert meta["_yoast_wpseo_title"] == "Home meta title"
    assert meta["_yoast_wpseo_metadesc"] == "Home meta description"
    assert meta["rank_math_title"] == "Home meta title"
    assert meta["rank_math_description"] == "Home meta description"
    assert "title" not in meta


def test_wordpress_dry_run_writes_meta_only_never_page_title():
    result = cms_connectors.apply_change(
        "wordpress",
        {},
        "https://acme.example/",
        {
            "title": "Calgary Plumber | Same-Day Service",
            "metaDescription": "Licensed Calgary plumbing repairs, same day.",
            "targetUrl": "/",
            "resource": "page",
        },
    )
    assert result["ok"] is True and result["dryRun"] is True
    payload = result["intended"]["payload"]
    assert "title" not in payload
    assert "excerpt" not in payload
    assert payload["meta"]["_yoast_wpseo_title"] == "Calgary Plumber | Same-Day Service"
    assert payload["meta"]["_yoast_wpseo_metadesc"] == "Licensed Calgary plumbing repairs, same day."
    assert payload["meta"]["rank_math_title"] == "Calgary Plumber | Same-Day Service"
    assert "_yoast_wpseo_opengraph-title" not in payload["meta"]
    assert result["includeOpenGraph"] is False
    assert result["includeJsonLd"] is False


def test_wordpress_dry_run_opt_in_open_graph_and_json_ld():
    result = cms_connectors.apply_change(
        "wordpress",
        {},
        "https://acme.example/",
        {
            "title": "Home meta title",
            "metaDescription": "Home meta description",
            "targetUrl": "https://acme.example/",
            "includeOpenGraph": True,
            "includeJsonLd": True,
        },
    )
    assert result["ok"] is True and result["dryRun"] is True
    payload = result["intended"]["payload"]
    assert payload["meta"]["_yoast_wpseo_opengraph-title"] == "Home meta title"
    assert payload["meta"]["rank_math_facebook_description"] == "Home meta description"
    assert payload["jsonLd"]["@type"] == "WebPage"
    assert payload["jsonLd"]["name"] == "Home meta title"
    assert "JSON-LD" in payload["contentNote"]


def test_build_webpage_json_ld_and_inject_replaces_prior_block():
    schema = cms_connectors.build_webpage_json_ld(
        meta_title="Acme Plumbing",
        meta_description="Same-day repairs.",
        page_url="https://acme.example/",
        site_name="acme.example",
    )
    assert schema["@type"] == "WebPage"
    assert schema["description"] == "Same-day repairs."
    first = cms_connectors.inject_json_ld_content("<p>Hello</p>", schema)
    assert "searchify-jsonld" in first and "application/ld+json" in first
    updated = cms_connectors.inject_json_ld_content(first, {**schema, "name": "Updated"})
    assert updated.count("<!--searchify-jsonld-->") == 1
    assert "Updated" in updated
    assert "<p>Hello</p>" in updated


def test_wordpress_live_publish_keeps_page_name_updates_meta(monkeypatch):
    class PostResponse:
        def __init__(self, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    state = {
        "pageName": "Home",
        "metaTitle": "Old meta title",
        "metaDesc": "Old meta description",
        "posts": [],
    }

    class LiveClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, auth=None, params=None):
            if "/settings" in url:
                return PostResponse(200, {"show_on_front": "page", "page_on_front": 5})
            if "/pages/5" in url:
                return PostResponse(
                    200,
                    {
                        "id": 5,
                        "link": "https://acme.example/",
                        "title": {"raw": state["pageName"]},
                        "meta": {
                            "_yoast_wpseo_title": state["metaTitle"],
                            "_yoast_wpseo_metadesc": state["metaDesc"],
                        },
                    },
                )
            return PostResponse(404, {})

        def post(self, url, auth=None, json=None):
            state["posts"].append(json or {})
            assert "title" not in (json or {}), "must not rename the WordPress page"
            assert "excerpt" not in (json or {}), "must not overwrite excerpt as page description"
            meta = (json or {}).get("meta") or {}
            if "_yoast_wpseo_title" in meta:
                state["metaTitle"] = meta["_yoast_wpseo_title"]
            if "_yoast_wpseo_metadesc" in meta:
                state["metaDesc"] = meta["_yoast_wpseo_metadesc"]
            return PostResponse(200, {"id": 5})

    monkeypatch.setattr(cms_connectors.httpx, "Client", LiveClient)
    result = cms_connectors.apply_change(
        "wordpress",
        {"username": "editor", "applicationPassword": "aaaa bbbb cccc dddd"},
        "https://acme.example/",
        {
            "title": "New meta title for Google",
            "metaDescription": "New meta description for Google.",
            "targetUrl": "https://acme.example/",
            "resource": "page",
        },
    )
    assert result["ok"] is True and result["dryRun"] is False
    assert result["pageNameUnchanged"] is True
    assert result["before"]["pageName"] == "Home"
    assert result["after"]["pageName"] == "Home"
    assert result["after"]["metaTitle"] == "New meta title for Google"
    assert result["after"]["metaDescription"] == "New meta description for Google."
    assert state["pageName"] == "Home"
    assert "title" not in state["posts"][0]
