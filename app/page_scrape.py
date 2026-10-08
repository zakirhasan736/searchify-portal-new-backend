"""Fetch live page title, H1, meta description, and visible copy for Sol."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

SKIP = {"script", "style", "noscript", "svg", "path"}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.h2_parts: list[str] = []
        self.text_parts: list[str] = []
        self.description = ""
        self.in_title = False
        self.in_h1 = False
        self.in_h2 = False
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        tag = (tag or "").lower()
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        if tag == "h1":
            self.in_h1 = True
        if tag == "h2" and len(self.h2_parts) < 8:
            self.in_h2 = True
        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = attr.get("content") or ""

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in SKIP and self.skip:
            self.skip -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag == "h1":
            self.in_h1 = False
        if tag == "h2":
            self.in_h2 = False

    def handle_data(self, data):
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


def scrape_page(url: str) -> dict:
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.netloc or " " in parsed.netloc:
        return {"url": raw, "title": "", "description": "", "h1": "", "headings": "", "text": "", "error": "invalid url"}
    try:
        response = httpx.get(
            raw,
            follow_redirects=True,
            timeout=8.0,
            headers={"User-Agent": "SearchifyBot/1.0 (+https://searchify.nextcreavo.com)"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return {"url": raw, "title": "", "description": "", "h1": "", "headings": "", "text": "", "error": str(exc)[:180]}

    parser = PageParser()
    try:
        parser.feed(response.text[:350_000])
    except Exception as exc:  # noqa: BLE001
        return {"url": str(response.url), "title": "", "description": "", "h1": "", "headings": "", "text": "", "error": str(exc)[:180]}

    title = " ".join(parser.title_parts).strip()
    h1 = " ".join(parser.h1_parts).strip()
    headings = " | ".join(part.strip() for part in parser.h2_parts if part.strip())
    text = " ".join(parser.text_parts).strip()
    return {
        "url": str(response.url),
        "title": title[:200],
        "description": (parser.description or "")[:400],
        "h1": h1[:200],
        "headings": headings[:500],
        "text": text[:3500],
        "error": "",
    }
