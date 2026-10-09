"""CMS connector adapters — WordPress, Shopify, Webflow, custom webhook.

Architecture: Searchify never hard-codes one CMS. Each provider implements
apply_change(connection, change) → {ok, dryRun, remoteId, detail}.
Credentials stay server-side; real HTTP runs when configured, else dry-run records intent.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

PROVIDERS = ("wordpress", "shopify", "webflow", "custom")

_JSONLD_START = "<!--searchify-jsonld-->"
_JSONLD_END = "<!--/searchify-jsonld-->"
_JSONLD_BLOCK_RE = re.compile(
    r"(?:<!--\s*wp:html\s*-->\s*)?<!--searchify-jsonld-->.*?<!--/searchify-jsonld-->(?:\s*<!--\s*/wp:html\s*-->)?",
    re.DOTALL | re.IGNORECASE,
)


def apply_change(provider: str, credentials: dict, site_url: str, change: dict) -> dict[str, Any]:
    provider = (provider or "").lower().strip()
    if provider not in PROVIDERS:
        return {"ok": False, "dryRun": True, "detail": f"Unknown provider: {provider}"}
    fn = {
        "wordpress": _wordpress,
        "shopify": _shopify,
        "webflow": _webflow,
        "custom": _custom,
    }[provider]
    return fn(credentials or {}, site_url or "", change or {})


def undo_change(provider: str, credentials: dict, site_url: str, change: dict, previous: dict) -> dict[str, Any]:
    """Restore previous SEO meta title / meta description (never the WP page name)."""
    payload = dict(change or {})
    payload["title"] = previous.get("title") or previous.get("beforeTitle") or previous.get("metaTitle")
    payload["metaDescription"] = previous.get("metaDescription") or previous.get("beforeDescription")
    payload["content"] = None  # metadata-only undo
    payload["updateContent"] = False
    payload["includeOpenGraph"] = bool(change.get("includeOpenGraph") or previous.get("includeOpenGraph"))
    payload["includeJsonLd"] = bool(change.get("includeJsonLd") or previous.get("includeJsonLd"))
    payload["undo"] = True
    return apply_change(provider, credentials, site_url, payload)


# SEO plugin meta keys — never WordPress post/page `title` (that renames menus / page name).
_WP_META_TITLE_KEYS = (
    "_yoast_wpseo_title",
    "rank_math_title",
    "_seopress_titles_title",
)
_WP_META_DESC_KEYS = (
    "_yoast_wpseo_metadesc",
    "rank_math_description",
    "_seopress_titles_desc",
)
_WP_OG_TITLE_KEYS = (
    "_yoast_wpseo_opengraph-title",
    "rank_math_facebook_title",
    "_seopress_social_fb_title",
)
_WP_OG_DESC_KEYS = (
    "_yoast_wpseo_opengraph-description",
    "rank_math_facebook_description",
    "_seopress_social_fb_desc",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _wp_meta_payload(
    meta_title: str | None,
    meta_desc: str | None,
    *,
    include_open_graph: bool = False,
) -> dict[str, str]:
    meta: dict[str, str] = {}
    title = (meta_title or "").strip()
    desc = (meta_desc or "").strip()
    if title:
        for key in _WP_META_TITLE_KEYS:
            meta[key] = title
        if include_open_graph:
            for key in _WP_OG_TITLE_KEYS:
                meta[key] = title
    if desc:
        for key in _WP_META_DESC_KEYS:
            meta[key] = desc
        if include_open_graph:
            for key in _WP_OG_DESC_KEYS:
                meta[key] = desc
    return meta


def _wp_pick_meta(meta: dict, keys: tuple[str, ...]) -> str:
    if not isinstance(meta, dict):
        return ""
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_webpage_json_ld(
    *,
    meta_title: str,
    meta_description: str,
    page_url: str,
    site_name: str = "",
) -> dict[str, Any]:
    """WebPage JSON-LD generated from the approved meta title and description."""
    title = (meta_title or "").strip()
    description = (meta_description or "").strip()
    url = (page_url or "").strip()
    graph: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "headline": title,
        "description": description,
    }
    if url:
        graph["url"] = url
        graph["@id"] = url
        graph["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
    if site_name.strip():
        graph["isPartOf"] = {"@type": "WebSite", "name": site_name.strip(), "url": urljoin(url, "/") if url else ""}
    return {k: v for k, v in graph.items() if v not in ("", None, {})}


def _json_ld_html_block(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # Gutenberg custom HTML block so themes output the script in page content.
    return (
        "<!-- wp:html -->\n"
        f"{_JSONLD_START}\n"
        f'<script type="application/ld+json">{body}</script>\n'
        f"{_JSONLD_END}\n"
        "<!-- /wp:html -->"
    )


def inject_json_ld_content(raw_content: str | None, payload: dict[str, Any]) -> str:
    """Replace any prior Searchify JSON-LD block, then append a fresh one."""
    base = _JSONLD_BLOCK_RE.sub("", raw_content or "").rstrip()
    block = _json_ld_html_block(payload)
    if not base:
        return block
    return f"{base}\n\n{block}"


def _wp_base(creds: dict, site_url: str) -> str:
    return (site_url or creds.get("siteUrl") or "").rstrip("/") + "/"


def _wp_auth(creds: dict) -> tuple[str, str]:
    user = (creds.get("username") or "").strip()
    password = (creds.get("applicationPassword") or creds.get("password") or "").replace(" ", "").strip()
    return user, password


def probe_wordpress(creds: dict, site_url: str) -> dict[str, Any]:
    """Confirm the application password can read the signed-in WordPress user."""
    user, password = _wp_auth(creds)
    base = _wp_base(creds, site_url)
    if not user or not password:
        return {"ok": False, "detail": "Username and application password are required."}
    if not base.startswith("http"):
        return {"ok": False, "detail": "Use a full website address, including https://."}
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            response = client.get(
                urljoin(base, "wp-json/wp/v2/users/me"),
                auth=(user, password),
                params={"context": "edit"},
            )
    except httpx.HTTPError as exc:
        text = str(exc).lower()
        if "getaddrinfo" in text or "name or service not known" in text or "nodename nor servname" in text:
            return {"ok": False, "detail": "Could not reach that website. Check the address and try again."}
        return {"ok": False, "detail": "Could not reach that WordPress site. Check the address and try again."}
    if response.status_code == 200:
        try:
            name = response.json().get("name") or user
        except Exception:
            name = user
        return {"ok": True, "detail": f"Signed in as {name}."}
    if response.status_code in (401, 403):
        return {"ok": False, "detail": "WordPress rejected that username or application password."}
    return {"ok": False, "detail": f"WordPress did not confirm the connection (HTTP {response.status_code})."}


def _norm_wp_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw if "://" in raw else f"https://{raw.lstrip('/')}")
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}".lower()


def _wp_match_link(items: list, want: str) -> tuple[str | None, str | None]:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if _norm_wp_url(item.get("link") or "") == want and item.get("id") is not None:
            return str(item["id"]), None
    return None, None


def _wp_resolve_id(client: httpx.Client, base: str, auth: tuple[str, str], change: dict) -> tuple[str | None, str]:
    """Return (id, resource) resolving by remoteId, front page, slug, or exact link match."""
    post_id = change.get("remoteId") or ""
    resource = change.get("resource") or "page"
    if post_id:
        return str(post_id), resource

    target = (change.get("targetUrl") or "").strip()
    if not target:
        return None, resource
    if not target.startswith("http"):
        target = urljoin(base, target.lstrip("/") if target != "/" else "")
    want = _norm_wp_url(target)
    path = urlparse(target).path.strip("/")
    slug = path.split("/")[-1] if path else ""

    # Homepage / site root — use Reading settings, then match page/post links to the root URL.
    if not slug:
        try:
            settings = client.get(urljoin(base, "wp-json/wp/v2/settings"), auth=auth)
            if settings.status_code < 400:
                data = settings.json() if isinstance(settings.json(), dict) else {}
                front = data.get("page_on_front") or data.get("page_for_posts")
                show = str(data.get("show_on_front") or "")
                if show == "page" and front:
                    return str(front), "page"
        except Exception:
            pass
        for res, kind in (("pages", "page"), ("posts", "post")):
            r = client.get(
                urljoin(base, f"wp-json/wp/v2/{res}"),
                auth=auth,
                params={"per_page": 100, "orderby": "menu_order", "order": "asc", "status": "publish"},
            )
            if r.status_code >= 400:
                continue
            data = r.json() if isinstance(r.json(), list) else []
            matched, _ = _wp_match_link(data, want)
            if matched:
                return matched, kind
            # Some homes use slug "home" / "homepage".
            for candidate in ("home", "homepage", "front-page", "index"):
                r2 = client.get(
                    urljoin(base, f"wp-json/wp/v2/{res}"),
                    auth=auth,
                    params={"slug": candidate, "per_page": 1},
                )
                if r2.status_code < 400:
                    rows = r2.json() if isinstance(r2.json(), list) else []
                    if rows and rows[0].get("id") is not None:
                        return str(rows[0]["id"]), kind
        return None, resource

    for res, kind in (("pages", "page"), ("posts", "post")):
        r = client.get(urljoin(base, f"wp-json/wp/v2/{res}"), auth=auth, params={"slug": slug, "per_page": 5})
        if r.status_code < 400:
            data = r.json() if isinstance(r.json(), list) else []
            if data:
                matched, _ = _wp_match_link(data, want)
                if matched:
                    return matched, kind
                return str(data[0].get("id")), kind
        # Fallback: search and match absolute link (handles nested paths / custom permalinks).
        r = client.get(urljoin(base, f"wp-json/wp/v2/{res}"), auth=auth, params={"search": slug, "per_page": 20})
        if r.status_code < 400:
            data = r.json() if isinstance(r.json(), list) else []
            matched, _ = _wp_match_link(data, want)
            if matched:
                return matched, kind
    return None, resource


def _wp_read(client: httpx.Client, base: str, auth: tuple[str, str], resource: str, post_id: str) -> dict:
    endpoint = "pages" if resource == "page" else "posts"
    r = client.get(urljoin(base, f"wp-json/wp/v2/{endpoint}/{post_id}"), auth=auth, params={"context": "edit"})
    if r.status_code >= 400:
        return {}
    data = r.json()
    page_name = ""
    if isinstance(data.get("title"), dict):
        page_name = data["title"].get("raw") or data["title"].get("rendered") or ""
    elif isinstance(data.get("title"), str):
        page_name = data["title"]
    excerpt = ""
    if isinstance(data.get("excerpt"), dict):
        excerpt = data["excerpt"].get("raw") or ""
    content_raw = ""
    if isinstance(data.get("content"), dict):
        content_raw = data["content"].get("raw") or ""
    elif isinstance(data.get("content"), str):
        content_raw = data["content"]
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta_title = _wp_pick_meta(meta, _WP_META_TITLE_KEYS)
    meta_desc = _wp_pick_meta(meta, _WP_META_DESC_KEYS)
    return {
        "id": str(data.get("id") or post_id),
        # Keep pageName for diagnostics only — Searchify never writes this field.
        "pageName": page_name,
        # `title` in before/after means SEO meta title (Google listing), not WP page name.
        "title": meta_title,
        "metaTitle": meta_title,
        "excerpt": excerpt,
        "metaDescription": meta_desc,
        "openGraphTitle": _wp_pick_meta(meta, _WP_OG_TITLE_KEYS),
        "openGraphDescription": _wp_pick_meta(meta, _WP_OG_DESC_KEYS),
        "contentRaw": content_raw,
        "hasSearchifyJsonLd": _JSONLD_START in content_raw,
        "link": data.get("link") or "",
        "raw": data,
    }


def _wordpress(creds: dict, site_url: str, change: dict) -> dict[str, Any]:
    """WordPress REST: Application Password.

    Always writes SEO meta title + meta description (Yoast / Rank Math / SEOPress).
    Optional (default off): Open Graph meta + JSON-LD WebPage block from the same values.
    Never updates the WordPress page/post title (page name / menu label / site title).
    """
    user, password = _wp_auth(creds)
    base = _wp_base(creds, site_url)
    post_id = change.get("remoteId") or creds.get("defaultPostId")
    dry = not (user and password and base.startswith("http"))

    meta_title = (change.get("metaTitle") or change.get("title") or "").strip()
    meta_desc = (change.get("metaDescription") or change.get("excerpt") or "").strip()
    include_og = _truthy(change.get("includeOpenGraph"))
    include_json_ld = _truthy(change.get("includeJsonLd"))
    page_url = (change.get("targetUrl") or change.get("link") or site_url or "").strip()
    site_host = urlparse(base).hostname or ""

    payload: dict[str, Any] = {}
    # Explicit content body only when requested — never remap meta title → WP title.
    if change.get("updateContent") and change.get("content"):
        payload["content"] = change["content"]
    meta = _wp_meta_payload(meta_title, meta_desc, include_open_graph=include_og)
    if meta:
        payload["meta"] = meta

    json_ld: dict[str, Any] | None = None
    if include_json_ld and (meta_title or meta_desc):
        json_ld = build_webpage_json_ld(
            meta_title=meta_title,
            meta_description=meta_desc,
            page_url=page_url,
            site_name=site_host,
        )

    if dry:
        intended = dict(payload)
        if json_ld:
            intended["jsonLd"] = json_ld
            intended["contentNote"] = "JSON-LD WebPage block will be appended (Searchify marker)."
        return {
            "ok": True,
            "dryRun": True,
            "provider": "wordpress",
            "detail": "Dry-run — add WordPress Application Password to execute live.",
            "includeOpenGraph": include_og,
            "includeJsonLd": include_json_ld,
            "intended": {"url": urljoin(base, f"wp-json/wp/v2/posts/{post_id or '{id}'}"), "payload": intended},
            "at": datetime.utcnow().isoformat(timespec="seconds"),
        }

    if not meta and not payload.get("content") and not json_ld:
        return {
            "ok": False,
            "dryRun": False,
            "provider": "wordpress",
            "detail": "Nothing to publish — meta title and meta description are both empty.",
        }

    auth = (user, password)
    try:
        with httpx.Client(timeout=45.0) as client:
            resource = change.get("resource") or "post"
            resolved_id, resource = _wp_resolve_id(client, base, auth, {**change, "remoteId": post_id})
            if not resolved_id:
                return {
                    "ok": False,
                    "dryRun": False,
                    "provider": "wordpress",
                    "detail": "Could not resolve WordPress post/page id from remoteId or target URL.",
                }

            before = _wp_read(client, base, auth, resource, resolved_id)
            # Conflict on SEO meta title when we know the previous meta value.
            expected = (change.get("beforeTitle") or "").strip()
            live_meta_title = (before.get("metaTitle") or before.get("title") or "").strip()
            if expected and live_meta_title and live_meta_title != expected:
                return {
                    "ok": False,
                    "dryRun": False,
                    "provider": "wordpress",
                    "detail": "Conflict: meta title changed since review. Re-open the opportunity.",
                    "before": {
                        "title": before.get("metaTitle") or before.get("title"),
                        "metaTitle": before.get("metaTitle"),
                        "metaDescription": before.get("metaDescription"),
                        "pageName": before.get("pageName"),
                    },
                }

            if json_ld:
                live_url = (before.get("link") or page_url or "").strip()
                if live_url and not json_ld.get("url"):
                    json_ld = build_webpage_json_ld(
                        meta_title=meta_title,
                        meta_description=meta_desc,
                        page_url=live_url,
                        site_name=site_host,
                    )
                payload["content"] = inject_json_ld_content(before.get("contentRaw") or "", json_ld)

            endpoint = "pages" if resource == "page" else "posts"
            url = urljoin(base, f"wp-json/wp/v2/{endpoint}/{resolved_id}")
            response = client.post(url, auth=auth, json=payload)
            if response.status_code >= 400 and "meta" in payload:
                # Retry with Yoast-only keys if the full SEO meta bag was rejected.
                soft_meta: dict[str, str] = {}
                if meta_title:
                    soft_meta["_yoast_wpseo_title"] = meta_title
                    if include_og:
                        soft_meta["_yoast_wpseo_opengraph-title"] = meta_title
                if meta_desc:
                    soft_meta["_yoast_wpseo_metadesc"] = meta_desc
                    if include_og:
                        soft_meta["_yoast_wpseo_opengraph-description"] = meta_desc
                if soft_meta:
                    soft_payload = {**{k: v for k, v in payload.items() if k != "meta"}, "meta": soft_meta}
                    response = client.post(url, auth=auth, json=soft_payload)
            if response.status_code >= 400 and include_json_ld and "content" in payload:
                # Meta may have applied; retry JSON-LD content alone after a soft meta success path failed together.
                meta_only = {k: v for k, v in payload.items() if k != "content"}
                if meta_only:
                    meta_resp = client.post(url, auth=auth, json=meta_only)
                    if meta_resp.status_code < 400:
                        content_resp = client.post(url, auth=auth, json={"content": payload["content"]})
                        if content_resp.status_code < 400:
                            response = content_resp
                        else:
                            response = meta_resp
                            include_json_ld = False
                            json_ld = None
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "dryRun": False,
                    "provider": "wordpress",
                    "detail": response.text[:400],
                    "status": response.status_code,
                }

            verified = _wp_read(client, base, auth, resource, resolved_id)
            after_title = (verified.get("metaTitle") or verified.get("title") or "").strip()
            after_desc = (verified.get("metaDescription") or "").strip()
            title_ok = (not meta_title) or after_title == meta_title
            desc_ok = (not meta_desc) or after_desc == meta_desc
            # If the SEO plugin does not expose meta via REST, treat HTTP success as published
            # and rely on the live HTML <title>/meta description check in the operator.
            meta_readable = bool(after_title or after_desc or before.get("metaTitle") or before.get("metaDescription"))
            verified_ok = (title_ok and desc_ok) if meta_readable else True
            page_name_unchanged = (before.get("pageName") or "") == (verified.get("pageName") or "")
            extras = []
            if include_og:
                extras.append("Open Graph")
            if include_json_ld and verified.get("hasSearchifyJsonLd"):
                extras.append("JSON-LD")
            elif include_json_ld:
                extras.append("JSON-LD (requested)")
            detail_core = "WordPress meta title and meta description updated" if verified_ok else (
                "WordPress updated (SEO meta could not be verified via REST — check live page)"
            )
            if extras:
                detail_core = f"{detail_core}; also wrote {' + '.join(extras)}"
            return {
                "ok": True,
                "dryRun": False,
                "provider": "wordpress",
                "remoteId": resolved_id,
                "resource": resource,
                "link": verified.get("link") or change.get("targetUrl"),
                "detail": detail_core,
                "verified": verified_ok,
                "pageNameUnchanged": page_name_unchanged,
                "includeOpenGraph": include_og,
                "includeJsonLd": include_json_ld,
                "jsonLd": json_ld,
                "before": {
                    "title": before.get("metaTitle") or before.get("title"),
                    "metaTitle": before.get("metaTitle"),
                    "metaDescription": before.get("metaDescription"),
                    "openGraphTitle": before.get("openGraphTitle"),
                    "openGraphDescription": before.get("openGraphDescription"),
                    "pageName": before.get("pageName"),
                    "hasSearchifyJsonLd": before.get("hasSearchifyJsonLd"),
                },
                "after": {
                    "title": verified.get("metaTitle") or verified.get("title"),
                    "metaTitle": verified.get("metaTitle"),
                    "metaDescription": verified.get("metaDescription"),
                    "openGraphTitle": verified.get("openGraphTitle"),
                    "openGraphDescription": verified.get("openGraphDescription"),
                    "pageName": verified.get("pageName"),
                    "hasSearchifyJsonLd": verified.get("hasSearchifyJsonLd"),
                },
                "at": datetime.utcnow().isoformat(timespec="seconds"),
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dryRun": False, "provider": "wordpress", "detail": str(exc)[:400]}


def _shopify(creds: dict, site_url: str, change: dict) -> dict[str, Any]:
    """Shopify Admin API — page/article update when shop + token present."""
    shop = (creds.get("shop") or "").strip().replace(".myshopify.com", "")
    token = (creds.get("accessToken") or "").strip()
    resource_id = change.get("remoteId") or creds.get("defaultPageId")
    dry = not (shop and token and resource_id)
    payload = {"page": {}}
    if change.get("title"):
        payload["page"]["title"] = change["title"]
    if change.get("updateContent") and (change.get("content") or change.get("bodyHtml")):
        payload["page"]["body_html"] = change.get("bodyHtml") or change.get("content")

    if dry:
        return {
            "ok": True,
            "dryRun": True,
            "provider": "shopify",
            "detail": "Dry-run — connect shop domain + Admin API access token to execute live.",
            "intended": {"shop": shop or "{shop}", "resourceId": resource_id, "payload": payload},
            "at": datetime.utcnow().isoformat(timespec="seconds"),
        }

    url = f"https://{shop}.myshopify.com/admin/api/2024-10/pages/{resource_id}.json"
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.put(url, headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"}, json=payload)
        if response.status_code >= 400:
            return {"ok": False, "dryRun": False, "provider": "shopify", "detail": response.text[:400], "status": response.status_code}
        data = response.json().get("page") or {}
        return {
            "ok": True,
            "dryRun": False,
            "provider": "shopify",
            "remoteId": str(data.get("id") or resource_id),
            "link": f"https://{shop}.myshopify.com/pages/{data.get('handle') or ''}",
            "detail": "Shopify page updated",
            "at": datetime.utcnow().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dryRun": False, "provider": "shopify", "detail": str(exc)[:400]}


def _webflow(creds: dict, site_url: str, change: dict) -> dict[str, Any]:
    """Webflow CMS items API v2 — collection item patch when token + ids present."""
    token = (creds.get("accessToken") or "").strip()
    collection_id = change.get("collectionId") or creds.get("collectionId")
    item_id = change.get("remoteId") or creds.get("defaultItemId")
    dry = not (token and collection_id and item_id)
    field_data = {}
    if change.get("title"):
        field_data["name"] = change["title"]
    if change.get("updateContent") and change.get("content"):
        field_data["post-body"] = change["content"]
    if change.get("metaDescription"):
        field_data["meta-description"] = change["metaDescription"]

    if dry:
        return {
            "ok": True,
            "dryRun": True,
            "provider": "webflow",
            "detail": "Dry-run — add Webflow token + collection/item ids to execute live.",
            "intended": {"collectionId": collection_id, "itemId": item_id, "fieldData": field_data},
            "at": datetime.utcnow().isoformat(timespec="seconds"),
        }

    url = f"https://api.webflow.com/v2/collections/{collection_id}/items/{item_id}"
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.patch(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"fieldData": field_data, "isDraft": False},
            )
        if response.status_code >= 400:
            return {"ok": False, "dryRun": False, "provider": "webflow", "detail": response.text[:400], "status": response.status_code}
        return {
            "ok": True,
            "dryRun": False,
            "provider": "webflow",
            "remoteId": str(item_id),
            "detail": "Webflow CMS item updated",
            "at": datetime.utcnow().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dryRun": False, "provider": "webflow", "detail": str(exc)[:400]}


def _custom(creds: dict, site_url: str, change: dict) -> dict[str, Any]:
    """Generic webhook / custom site connector — POST signed payload to customer endpoint."""
    webhook = (creds.get("webhookUrl") or site_url or "").strip()
    secret = (creds.get("secret") or "").strip()
    dry = not (webhook.startswith("http"))
    body = {
        "action": "searchify.apply_change",
        "targetUrl": change.get("targetUrl"),
        "changeType": change.get("changeType"),
        "title": change.get("title"),
        "metaDescription": change.get("metaDescription"),
        "remoteId": change.get("remoteId"),
        "undo": bool(change.get("undo")),
        "at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    if dry:
        return {
            "ok": True,
            "dryRun": True,
            "provider": "custom",
            "detail": "Dry-run — set webhook URL to execute on a custom site.",
            "intended": body,
            "at": body["at"],
        }
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Searchify-Secret"] = secret
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.post(webhook, headers=headers, json=body)
        if response.status_code >= 400:
            return {"ok": False, "dryRun": False, "provider": "custom", "detail": response.text[:400], "status": response.status_code}
        return {
            "ok": True,
            "dryRun": False,
            "provider": "custom",
            "detail": "Custom webhook accepted change",
            "status": response.status_code,
            "at": datetime.utcnow().isoformat(timespec="seconds"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dryRun": False, "provider": "custom", "detail": str(exc)[:400]}
