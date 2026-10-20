"""Image, script, and CSS weight from a crawl. Byte figures are used only when the crawl stored them."""

from __future__ import annotations

import base64
import ipaddress
from io import BytesIO
from urllib.parse import urljoin, urlparse

IMAGE_TOTAL = 500_000
SCRIPT_TOTAL = 300_000
CSS_TOTAL = 150_000
SINGLE_IMAGE = 200_000
MAX_DOWNLOAD = 5_000_000


def _as_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(float(str(value).replace(",", "")))
    except ValueError:
        return None
    return number if number >= 0 else None


def _first_int(item: dict, meta: dict, key: str) -> int | None:
    return _as_int(meta.get(key)) if _as_int(meta.get(key)) is not None else _as_int(item.get(key))


def page_sizes(item: dict) -> dict:
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    return {
        "images": _first_int(item, meta, "images_size"),
        "scripts": _first_int(item, meta, "scripts_size"),
        "css": _first_int(item, meta, "stylesheets_size"),
    }


def kb(size: int) -> str:
    return f"{max(1, round(size / 1000)):,} KB"


def weight_issues(items: list[dict]) -> list[dict]:
    """Findings only where the crawl stored a byte size over the limit."""
    images, assets = [], []
    for item in items or []:
        if item.get("resource_type") not in (None, "", "html"):
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        sizes = page_sizes(item)
        if sizes["images"] is not None and sizes["images"] >= IMAGE_TOTAL:
            images.append({
                "url": url,
                "imagesBytes": sizes["images"],
                "detail": (
                    f"Images on this page total {kb(sizes['images'])}. "
                    "A photo over 200 KB can be prepared as a smaller JPEG on an approval card. "
                    "The card does not upload the file to the site."
                ),
            })
        parts = []
        if sizes["scripts"] is not None and sizes["scripts"] >= SCRIPT_TOTAL:
            parts.append(f"scripts {kb(sizes['scripts'])}")
        if sizes["css"] is not None and sizes["css"] >= CSS_TOTAL:
            parts.append(f"CSS {kb(sizes['css'])}")
        if parts:
            assets.append({
                "url": url,
                "scriptsBytes": sizes["scripts"] or 0,
                "cssBytes": sizes["css"] or 0,
                "detail": (
                    f"This page is carrying {' and '.join(parts)}. "
                    "Searchify can put that in the approval queue. It does not edit the theme files."
                ),
            })
    images.sort(key=lambda row: row["imagesBytes"], reverse=True)
    assets.sort(key=lambda row: (row["scriptsBytes"] + row["cssBytes"]), reverse=True)
    findings = []
    if images:
        lead = images[0]
        findings.append({
            "key": "heavy_images",
            "name": "Images are heavy",
            "severity": "Warning",
            "fix": "Prepare a smaller image on the approval card. Nothing is uploaded until a later release can apply an approved file.",
            "meta": False,
            "prepare": "image",
            "area": "Images",
            "weight": {"imagesBytes": lead["imagesBytes"], "url": lead["url"]},
            "steps": [
                "Open the page named here.",
                "Prepare a smaller image. Searchify keeps the JPEG on the approval card.",
                "Approve the card to keep that file. It is not published to the site from this step.",
            ],
            "count": len(images),
            "pages": [{"url": row["url"], "status": None, "title": "", "description": "", "detail": row["detail"]} for row in images[:25]],
        })
    if assets:
        lead = assets[0]
        findings.append({
            "key": "heavy_assets",
            "name": "Scripts or CSS are heavy",
            "severity": "Warning",
            "fix": "Queue the page so the weight is on an approval card. Searchify does not remove scripts or stylesheets from the theme.",
            "meta": False,
            "prepare": "assets",
            "area": "Page",
            "weight": {"scriptsBytes": lead["scriptsBytes"], "cssBytes": lead["cssBytes"], "url": lead["url"]},
            "steps": [
                "Read the script and CSS size on this card.",
                "Ask the site builder to load the heavy files after the page, or remove one that the page does not need.",
                "Approving the card records the request. It does not change the theme.",
            ],
            "count": len(assets),
            "pages": [{"url": row["url"], "status": None, "title": "", "description": "", "detail": row["detail"]} for row in assets[:25]],
        })
    return findings


def same_site(page_url: str, image_url: str) -> bool:
    absolute = urljoin(page_url, image_url or "")
    page = urlparse(page_url)
    image = urlparse(absolute)
    if image.scheme not in {"http", "https"} or not image.hostname or not page.hostname:
        return False
    if _blocked(image.hostname):
        return False
    return page.hostname.lower().removeprefix("www.") == image.hostname.lower().removeprefix("www.")


def _blocked(host: str) -> bool:
    name = (host or "").strip().lower().rstrip(".")
    if name in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def compress_image(data: bytes, max_bytes: int = SINGLE_IMAGE) -> tuple[bytes, str]:
    from PIL import Image

    image = Image.open(BytesIO(data))
    image = image.convert("RGB")
    image.thumbnail((1600, 1600))
    quality = 82
    encoded = b""
    while quality >= 40:
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = buf.getvalue()
        if len(encoded) <= max_bytes:
            break
        quality -= 8
    if len(encoded) > max_bytes:
        image.thumbnail((1000, 1000))
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=40, optimize=True)
        encoded = buf.getvalue()
    return encoded, "image/jpeg"


def smaller_image(page_url: str, image_urls: list[str]) -> dict:
    """Download the heaviest same-site image over 200 KB and attach a smaller JPEG."""
    candidates = [urljoin(page_url, src) for src in image_urls if same_site(page_url, src)]
    if not candidates:
        return {"note": "No image on this page could be read from the same site."}
    import httpx

    heaviest = ""
    heaviest_size = 0
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        for src in candidates[:8]:
            try:
                head = client.head(src)
                final = str(head.url)
                if not same_site(page_url, final):
                    continue
                length = _as_int(head.headers.get("content-length")) or 0
            except Exception:  # noqa: BLE001
                length = 0
            if length > heaviest_size:
                heaviest, heaviest_size = src, length
        target = heaviest or candidates[0]
        if heaviest_size and heaviest_size <= SINGLE_IMAGE:
            return {"note": f"The largest image checked is already {kb(heaviest_size)}.", "source": target, "beforeBytes": heaviest_size}
        try:
            response = client.get(target)
        except Exception as exc:  # noqa: BLE001
            return {"note": f"The image could not be downloaded. {exc}"[:240], "source": target}
        final = str(response.url)
        if not same_site(page_url, final) or response.status_code >= 400:
            return {"note": "The image request left this site or did not succeed.", "source": target}
        data = response.content[: MAX_DOWNLOAD + 1]
        if len(data) > MAX_DOWNLOAD:
            return {"note": "That image is over 5 MB, so it was not downloaded.", "source": target}
        content_type = (response.headers.get("content-type") or "").lower()
        if "image/" not in content_type and not target.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return {"note": "The address did not return an image.", "source": target}
    try:
        smaller, mime = compress_image(data)
    except Exception as exc:  # noqa: BLE001
        return {"note": f"The image could not be reduced. {exc}"[:240], "source": target, "beforeBytes": len(data)}
    return {
        "source": target,
        "beforeBytes": len(data),
        "afterBytes": len(smaller),
        "smallerType": mime,
        "smallerImage": base64.b64encode(smaller).decode(),
        "note": f"Reduced from {kb(len(data))} to {kb(len(smaller))}. The file stays on this card.",
    }
