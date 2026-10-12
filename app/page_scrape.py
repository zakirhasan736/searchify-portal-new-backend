"""Fetch the live listing signals on one page: title, description, Open Graph, JSON-LD, H1, and copy."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

SKIP = {"script", "style", "noscript", "svg", "path"}
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "wistia.com", "loom.com")


def is_video_src(value: str) -> bool:
    low = (value or "").lower().split("?")[0]
    return any(host in low for host in _VIDEO_HOSTS) or low.endswith((".mp4", ".webm", ".ogg"))


def summarize_json_ld(blocks: list[str]) -> dict:
    """Pull the type, name, and description already published in JSON-LD."""
    types: list[str] = []
    names: list[str] = []
    descriptions: list[str] = []
    faqs: list[str] = []
    rating = ""
    count = ""

    def walk(node) -> None:
        nonlocal rating, count
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        graph = node.get("@graph")
        if graph is not None:
            walk(graph)
        kind = node.get("@type")
        if isinstance(kind, list):
            kind = next((part for part in kind if part), "")
        if kind and str(kind) not in types:
            types.append(str(kind))
        for key in ("name", "headline"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
                break
        desc = node.get("description")
        if isinstance(desc, str) and desc.strip():
            descriptions.append(desc.strip())
        aggregate = node.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating = str(aggregate.get("ratingValue") or rating or "")
            count = str(aggregate.get("reviewCount") or aggregate.get("ratingCount") or count or "")
        if str(kind) in {"Review", "AggregateRating"} and not rating:
            rating = str(node.get("ratingValue") or "")
            count = str(node.get("reviewCount") or node.get("ratingCount") or count or "")
        if str(kind) in {"FAQPage", "Question"} or node.get("acceptedAnswer"):
            question = node.get("name") if str(kind) == "Question" or node.get("acceptedAnswer") else ""
            if isinstance(question, str) and question.strip().endswith("?") and question.strip() not in faqs:
                faqs.append(question.strip()[:180])
        entities = node.get("mainEntity")
        if entities is not None:
            walk(entities)

    for raw in blocks[:6]:
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return {
        "schemaTypes": types[:8],
        "schemaName": (names[0] if names else "")[:200],
        "schemaDescription": (descriptions[0] if descriptions else "")[:400],
        "faqs": faqs[:8],
        "reviewRating": rating[:20],
        "reviewCount": count[:20],
    }


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.h2_parts: list[str] = []
        self.text_parts: list[str] = []
        self.json_ld: list[str] = []
        self.description = ""
        self.og_title = ""
        self.og_description = ""
        self.og_type = ""
        self.og_image = ""
        self.images: list[dict] = []
        self.canonical = ""
        self.nav: list[str] = []
        self.faqs: list[str] = []
        self.videos: list[str] = []
        self.nav_depth = 0
        self.in_nav_link = False
        self.nav_buf: list[str] = []
        self.in_title = False
        self.in_h1 = False
        self.in_h2 = False
        self.in_jsonld = False
        self.json_buf: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attr = {str(k).lower(): (v or "") for k, v in attrs}
        tag = (tag or "").lower()
        if tag == "script" and "ld+json" in attr.get("type", "").lower() and len(self.json_ld) < 6:
            self.in_jsonld = True
            self.json_buf = []
            return
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip or self.in_jsonld:
            return
        if tag == "title":
            self.in_title = True
        if tag == "h1":
            self.in_h1 = True
        if tag == "h2" and len(self.h2_parts) < 8:
            self.in_h2 = True
        if tag == "nav":
            self.nav_depth += 1
        if tag == "a" and self.nav_depth and len(self.nav) < 12:
            self.in_nav_link = True
            self.nav_buf = []
        if tag in {"iframe", "video", "source"}:
            src = attr.get("src") or ""
            if is_video_src(src) and src not in self.videos and len(self.videos) < 6:
                self.videos.append(src[:300])
        if tag == "link" and "canonical" in attr.get("rel", "").lower():
            self.canonical = attr.get("href") or ""
        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            content = attr.get("content") or ""
            if name == "description" and not self.description:
                self.description = content
            elif name == "og:title" and not self.og_title:
                self.og_title = content
            elif name == "og:description" and not self.og_description:
                self.og_description = content
            elif name == "og:type" and not self.og_type:
                self.og_type = content
            elif name == "og:image" and not self.og_image:
                self.og_image = content
            elif name in {"og:video", "og:video:url"} and is_video_src(content) and content not in self.videos:
                self.videos.append(content[:300])
        if tag == "img" and len(self.images) < 8:
            src = attr.get("src") or attr.get("data-src") or ""
            if src and not src.startswith("data:"):
                self.images.append({"src": src[:300], "alt": (attr.get("alt") or "")[:160]})

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self.in_jsonld and tag != "script":
            return
        if tag == "script" and self.in_jsonld:
            self.json_ld.append("".join(self.json_buf))
            self.in_jsonld = False
            self.json_buf = []
            return
        if tag in SKIP and self.skip:
            self.skip -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag == "h1":
            self.in_h1 = False
        if tag == "nav" and self.nav_depth:
            self.nav_depth -= 1
        if tag == "a" and self.in_nav_link:
            label = " ".join(" ".join(self.nav_buf).split())
            if label and label not in self.nav and len(self.nav) < 12:
                self.nav.append(label[:80])
            self.in_nav_link = False
            self.nav_buf = []
        if tag == "h2" and self.h2_parts:
            heading = self.h2_parts[-1].strip()
            if heading.endswith("?") and heading not in self.faqs and len(self.faqs) < 8:
                self.faqs.append(heading[:180])
            self.in_h2 = False

    def handle_data(self, data):
        if self.in_jsonld:
            self.json_buf.append(data or "")
            return
        if self.in_nav_link:
            chunk = " ".join((data or "").split())
            if chunk:
                self.nav_buf.append(chunk)
            return
        if self.skip:
            return
        chunk = " ".join((data or "").split())
        if not chunk:
            return
        if self.in_title:
            self.title_parts.append(chunk)
        elif self.in_h1:
            self.h1_parts.append(chunk)
        elif self.in_h2:
            self.h2_parts.append(chunk)
            self.in_h2 = False
        elif len(" ".join(self.text_parts)) < 4000:
            self.text_parts.append(chunk)


def listing_from_parser(parser: PageParser) -> dict:
    title = " ".join(parser.title_parts).strip()
    h1 = " ".join(parser.h1_parts).strip()
    headings = " | ".join(part.strip() for part in parser.h2_parts if part.strip())
    text = " ".join(parser.text_parts).strip()
    schema = summarize_json_ld(parser.json_ld)
    description = (parser.description or parser.og_description or "").strip()
    faqs = list(dict.fromkeys([*(schema.get("faqs") or []), *parser.faqs]))[:8]
    return {
        "title": title[:200],
        "description": description[:400],
        "ogTitle": (parser.og_title or "")[:200],
        "ogDescription": (parser.og_description or "")[:400],
        "ogType": (parser.og_type or "")[:40],
        "ogImage": (parser.og_image or "")[:300],
        "images": parser.images[:8],
        "canonical": (parser.canonical or "")[:300],
        "nav": parser.nav[:12],
        "faqs": faqs,
        "videos": parser.videos[:6],
        "h1": h1[:200],
        "headings": headings[:500],
        "text": text[:3500],
        **schema,
        "faqs": faqs,
        "reviewRating": schema.get("reviewRating") or "",
        "reviewCount": schema.get("reviewCount") or "",
    }


def scrape_page(url: str) -> dict:
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    empty = {
        "url": raw, "title": "", "description": "", "ogTitle": "", "ogDescription": "", "ogType": "", "ogImage": "", "images": [],
        "h1": "", "headings": "", "text": "", "schemaTypes": [], "schemaName": "", "schemaDescription": "", "error": "",
    }
    if not parsed.netloc or " " in parsed.netloc:
        return {**empty, "error": "invalid url"}
    try:
        response = httpx.get(
            raw,
            follow_redirects=True,
            timeout=8.0,
            headers={"User-Agent": "SearchifyBot/1.0 (+https://searchify.nextcreavo.com)"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return {**empty, "error": str(exc)[:180]}

    parser = PageParser()
    try:
        parser.feed(response.text[:350_000])
    except Exception as exc:  # noqa: BLE001
        return {**empty, "url": str(response.url), "error": str(exc)[:180]}

    return {"url": str(response.url), **listing_from_parser(parser), "error": ""}


def suggest_images(images: list | None, topic: str, page_url: str) -> list[dict]:
    """Alt text for the photos on the page. Existing alt is kept. Empty alt gets words from the page topic."""
    rows = []
    subject = " ".join((topic or "This page").split())[:80]
    for img in images or []:
        raw = str((img or {}).get("src") or "").strip()
        if not raw or raw.startswith("data:"):
            continue
        src = raw if raw.startswith("http") else urljoin(page_url or "", raw)
        alt = " ".join(str((img or {}).get("alt") or "").split())[:160]
        stem = src.rstrip("/").split("/")[-1].split("?")[0].rsplit(".", 1)[0]
        name = " ".join(re.sub(r"[-_]+", " ", re.sub(r"\d+", " ", stem)).split())
        if len(name) < 3:
            name = ""
        suggested = alt or (f"{subject} — {name}" if name else f"{subject} photo")
        rows.append({
            "src": src[:300],
            "currentAlt": alt,
            "suggestedAlt": suggested[:140],
            "needsAlt": not bool(alt),
        })
        if len(rows) >= 6:
            break
    return rows
