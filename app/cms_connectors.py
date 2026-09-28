"""CMS connector adapters — WordPress, Shopify, Webflow, custom webhook.

Architecture: Searchify never hard-codes one CMS. Each provider implements
apply_change(connection, change) → {ok, dryRun, remoteId, detail}.
Credentials stay server-side; real HTTP runs when configured, else dry-run records intent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

PROVIDERS = ("wordpress", "shopify", "webflow", "custom")


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
    """Restore previous title/description values."""
    payload = dict(change or {})
    payload["title"] = previous.get("title") or previous.get("beforeTitle")
    payload["metaDescription"] = previous.get("metaDescription") or previous.get("beforeDescription")
    payload["content"] = None  # metadata-only undo
    payload["excerpt"] = payload.get("metaDescription")
    payload["undo"] = True
    return apply_change(provider, credentials, site_url, payload)


def _wp_base(creds: dict, site_url: str) -> str:
    return (site_url or creds.get("siteUrl") or "").rstrip("/") + "/"


def _wp_auth(creds: dict) -> tuple[str, str]:
    user = (creds.get("username") or "").strip()
    password = (creds.get("applicationPassword") or creds.get("password") or "").strip()
    return user, password


def _wp_resolve_id(client: httpx.Client, base: str, auth: tuple[str, str], change: dict) -> tuple[str | None, str]:
    """Return (id, resource) resolving by remoteId or target URL slug."""
    post_id = change.get("remoteId") or ""
    resource = change.get("resource") or "post"
    if post_id:
        return str(post_id), resource

    target = (change.get("targetUrl") or "").strip()
    if not target:
        return None, resource

    path = urlparse(target).path.strip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        return None, resource

    for res in ("pages", "posts"):
        r = client.get(urljoin(base, f"wp-json/wp/v2/{res}"), auth=auth, params={"slug": slug, "per_page": 1})
        if r.status_code < 400:
            data = r.json()
            if isinstance(data, list) and data:
                return str(data[0].get("id")), ("page" if res == "pages" else "post")
    return None, resource


def _wp_read(client: httpx.Client, base: str, auth: tuple[str, str], resource: str, post_id: str) -> dict:
    endpoint = "pages" if resource == "page" else "posts"
    r = client.get(urljoin(base, f"wp-json/wp/v2/{endpoint}/{post_id}"), auth=auth, params={"context": "edit"})
    if r.status_code >= 400:
        return {}
    data = r.json()
    title = ""
    if isinstance(data.get("title"), dict):
        title = data["title"].get("raw") or data["title"].get("rendered") or ""
    elif isinstance(data.get("title"), str):
        title = data["title"]
    excerpt = ""
    if isinstance(data.get("excerpt"), dict):
        excerpt = data["excerpt"].get("raw") or ""
    yoast = ""
    meta = data.get("meta") or {}
    if isinstance(meta, dict):
        yoast = meta.get("_yoast_wpseo_metadesc") or meta.get("rank_math_description") or ""
    return {
        "id": str(data.get("id") or post_id),
        "title": title,
        "excerpt": excerpt,
        "metaDescription": yoast or excerpt,
        "link": data.get("link") or "",
        "raw": data,
    }


def _wordpress(creds: dict, site_url: str, change: dict) -> dict[str, Any]:
    """WordPress REST: Application Password. Metadata-first (title + excerpt/Yoast)."""
    user, password = _wp_auth(creds)
    base = _wp_base(creds, site_url)
    post_id = change.get("remoteId") or creds.get("defaultPostId")
    dry = not (user and password and base.startswith("http"))

    payload: dict[str, Any] = {}
    if change.get("title"):
        payload["title"] = change["title"]
    # v1: titles & descriptions only — do not push full content unless explicitly requested
    if change.get("updateContent") and change.get("content"):
        payload["content"] = change["content"]
    meta_desc = change.get("excerpt") or change.get("metaDescription")
    if meta_desc:
        payload["excerpt"] = meta_desc
        payload["meta"] = {"_yoast_wpseo_metadesc": meta_desc}

    if dry:
        return {
            "ok": True,
            "dryRun": True,
            "provider": "wordpress",
            "detail": "Dry-run — add WordPress Application Password to execute live.",
            "intended": {"url": urljoin(base, f"wp-json/wp/v2/posts/{post_id or '{id}'}"), "payload": payload},
            "at": datetime.utcnow().isoformat(timespec="seconds"),
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
            # Conflict check: if expected before title provided and live differs, abort
            expected = (change.get("beforeTitle") or "").strip()
            if expected and before.get("title") and before["title"].strip() != expected:
                return {
                    "ok": False,
                    "dryRun": False,
                    "provider": "wordpress",
                    "detail": "Conflict: page title changed since review. Re-open the opportunity.",
                    "before": {"title": before.get("title"), "metaDescription": before.get("metaDescription")},
                }

            endpoint = "pages" if resource == "page" else "posts"
            url = urljoin(base, f"wp-json/wp/v2/{endpoint}/{resolved_id}")
            response = client.post(url, auth=auth, json=payload)
            if response.status_code >= 400:
                # Retry without Yoast meta if meta write rejected
                if "meta" in payload:
                    soft = {k: v for k, v in payload.items() if k != "meta"}
                    response = client.post(url, auth=auth, json=soft)
                if response.status_code >= 400:
                    return {
                        "ok": False,
                        "dryRun": False,
                        "provider": "wordpress",
                        "detail": response.text[:400],
                        "status": response.status_code,
                    }

            verified = _wp_read(client, base, auth, resource, resolved_id)
            title_ok = not change.get("title") or (verified.get("title") or "").strip() == str(change.get("title")).strip()
            return {
                "ok": True,
                "dryRun": False,
                "provider": "wordpress",
                "remoteId": resolved_id,
                "resource": resource,
                "link": verified.get("link") or change.get("targetUrl"),
                "detail": "WordPress metadata updated and verified" if title_ok else "WordPress updated (verify mismatch on title)",
                "verified": title_ok,
                "before": {
                    "title": before.get("title"),
                    "metaDescription": before.get("metaDescription"),
                },
                "after": {
                    "title": verified.get("title"),
                    "metaDescription": verified.get("metaDescription"),
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
