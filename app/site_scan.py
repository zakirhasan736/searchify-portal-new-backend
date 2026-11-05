"""Read a whole website, understand the business, and compare the competitors that rank for each page.

1. Discover pages from robots.txt sitemaps, WordPress sitemaps, and internal links.
2. Read each page: title, description, H1, H2/H3, copy, canonical, noindex.
3. One AI pass turns the pages plus the onboarding brief into a business summary and a page map
   (role and the search the page should win).
4. Competitors per search: DataForSEO SERP when configured, else Google Places websites already synced.
   Each competitor page is read for its title, description, and headings.

Nothing here invents a rank, a volume, or a competitor. An empty source stays empty.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import logging

import httpx

from app.openai_client import chat_text, openai_configured
from app.page_scrape import is_video_src, summarize_json_ld

log = logging.getLogger("searchify.site_scan")
AGENT = "SearchifyBot/1.0 (+https://searchify.nextcreavo.com)"
SKIP_TAGS = {"script", "style", "noscript", "svg", "path", "template"}
CHROME_TAGS = {"nav", "footer", "header", "aside", "form"}
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|svg|ico|css|js|json|xml|zip|rar|mp4|mp3|mov|avi|docx?|xlsx?|pptx?|woff2?|ttf)$", re.I)
SKIP_PATH = re.compile(
    r"/(wp-admin|wp-login|wp-json|cart|checkout|my-account|account|login|logout|register|signin|signup|feed|"
    r"tag|author|search|xmlrpc|cdn-cgi|trackback|comments|embed|attachment)(/|$)|/page/\d+/?$",
    re.I,
)
LEGAL = re.compile(r"(privacy|cookie|terms|disclaimer|refund|shipping-policy|legal|gdpr|accessibility)", re.I)


def normalize(url: str) -> str:
    parts = urlparse(url)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, "", "", ""))


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def public_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if not host or host in {"localhost"}:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _get(client: httpx.Client, url: str, timeout: float = 10.0) -> httpx.Response | None:
    try:
        response = client.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    return response


class PageReader(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title: list[str] = []
        self.h1: list[str] = []
        self.headings: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self.description = ""
        self.og_title = ""
        self.og_description = ""
        self.og_type = ""
        self.og_image = ""
        self.site_name = ""
        self.lang = ""
        self.images: list[dict] = []
        self.nav: list[str] = []
        self.faqs: list[str] = []
        self.videos: list[str] = []
        self.nav_depth = 0
        self.in_nav_link = False
        self.nav_buf: list[str] = []
        self.json_ld: list[str] = []
        self.in_jsonld = False
        self.json_buf: list[str] = []
        self.canonical = ""
        self.noindex = False
        self.skip = 0
        self.chrome = 0
        self.current = ""
        self.buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and not self.lang:
            self.lang = (attr.get("lang") or "")[:24]
        if tag == "script" and "ld+json" in attr.get("type", "").lower() and len(self.json_ld) < 6:
            self.in_jsonld = True
            self.json_buf = []
            return
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.in_jsonld:
            return
        if tag == "nav":
            self.nav_depth += 1
        if tag == "a" and self.nav_depth and len(self.nav) < 12:
            self.in_nav_link = True
            self.nav_buf = []
        if tag in {"iframe", "video", "source"}:
            src = attr.get("src") or ""
            if is_video_src(src) and src not in self.videos and len(self.videos) < 6:
                self.videos.append(src[:300])
        if tag in CHROME_TAGS:
            self.chrome += 1
        if tag == "a" and attr.get("href"):
            self.links.append(attr["href"])
        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            content = attr.get("content", "")
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
            elif name == "og:site_name" and not self.site_name:
                self.site_name = content
            elif name in {"og:video", "og:video:url"} and is_video_src(content) and content not in self.videos:
                self.videos.append(content[:300])
            if name == "robots" and "noindex" in attr.get("content", "").lower():
                self.noindex = True
        if tag == "link" and "canonical" in attr.get("rel", "").lower():
            self.canonical = attr.get("href", "")
        if tag == "img" and len(self.images) < 8 and not self.skip:
            src = attr.get("src") or attr.get("data-src") or ""
            if src and not src.startswith("data:"):
                self.images.append({"src": src[:300], "alt": (attr.get("alt") or "")[:160]})
        if tag in {"title", "h1", "h2", "h3"} and not self.skip:
            self.current = tag
            self.buffer = []

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self.in_jsonld and tag != "script":
            return
        if tag == "script" and self.in_jsonld:
            self.json_ld.append("".join(self.json_buf))
            self.in_jsonld = False
            self.json_buf = []
            return
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
            return
        if tag in CHROME_TAGS and self.chrome:
            self.chrome -= 1
        if tag == "nav" and self.nav_depth:
            self.nav_depth -= 1
        if tag == "a" and self.in_nav_link:
            label = " ".join(" ".join(self.nav_buf).split())
            if label and label not in self.nav and len(self.nav) < 12:
                self.nav.append(label[:80])
            self.in_nav_link = False
            self.nav_buf = []
        if tag == self.current:
            value = " ".join(" ".join(self.buffer).split())
            if value:
                if tag == "title":
                    self.title.append(value)
                elif tag == "h1":
                    self.h1.append(value)
                elif len(self.headings) < 14:
                    self.headings.append(value)
                    if value.endswith("?") and value not in self.faqs and len(self.faqs) < 8:
                        self.faqs.append(value[:180])
            self.current = ""
            self.buffer = []

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
        if self.current:
            self.buffer.append(chunk)
            return
        if not self.chrome and sum(len(part) for part in self.text) < 6000:
            self.text.append(chunk)


def page_key(url: str) -> str:
    """Dedupe key: scheme-less, www-less, no trailing slash."""
    parts = urlparse(normalize(url))
    return f"{(parts.netloc or '').removeprefix('www.')}{parts.path.rstrip('/') or '/'}"


def read_page(client: httpx.Client, url: str, timeout: float = 10.0) -> dict:
    fetched = datetime.utcnow().isoformat(timespec="seconds")
    try:
        response = client.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        return {"url": url, "requestedUrl": url, "error": f"could not load ({type(exc).__name__})", "status": None, "fetchedAt": fetched}
    final = str(response.url)
    base = {"url": normalize(final), "requestedUrl": url, "status": response.status_code, "fetchedAt": fetched,
            "redirected": page_key(final) != page_key(url)}
    if response.status_code >= 400:
        return {**base, "url": url, "error": f"HTTP {response.status_code}"}
    kind = response.headers.get("content-type", "")
    if "html" not in kind:
        return {**base, "error": "not html"}
    reader = PageReader(final)
    try:
        reader.feed(response.text[:600_000])
    except Exception:  # noqa: BLE001
        pass
    html_slice = response.text[:600_000]
    text = " ".join(reader.text)
    try:
        from app.audit_checks import analyze_html

        audit = analyze_html(html_slice, final, text)
    except Exception:  # noqa: BLE001 - audit facts are extra; never fail the page read
        audit = None
    lower_html = html_slice.lower()
    script_count = lower_html.count("<script")
    img_count = lower_html.count("<img")
    img_sized = len(re.findall(r"<img[^>]*(?:width=|height=)", lower_html[:250_000], flags=re.I))
    img_lazy = len(re.findall(r"<img[^>]*loading=['\"]lazy['\"]", lower_html[:250_000], flags=re.I))
    img_srcset = lower_html[:250_000].count("srcset=")
    class_bits = re.findall(r'class=["\']([^"\']+)["\']', html_slice[:120_000], flags=re.I)
    render_hints = {
        "nextData": "__NEXT_DATA__" in html_slice,
        "nextRoot": 'id="__next"' in lower_html or "id='__next'" in lower_html,
        "reactRoot": "data-reactroot" in lower_html or "data-react-helmet" in lower_html,
        "vue": "data-v-" in lower_html[:80_000],
        "angular": "ng-version" in lower_html,
        "scriptTags": script_count,
        "imgTags": img_count,
        "imgSized": img_sized,
        "imgLazy": img_lazy,
        "imgSrcset": img_srcset,
        "bytes": len(html_slice.encode("utf-8", errors="ignore")),
        "classSample": " ".join(class_bits[:80])[:4000],
    }
    title = " ".join(reader.title)[:200]
    description = (reader.description or reader.og_description).strip()[:400]
    schema = summarize_json_ld(reader.json_ld)
    canonical = urljoin(final, reader.canonical) if reader.canonical else ""
    faqs = list(dict.fromkeys([*(schema.get("faqs") or []), *reader.faqs]))[:8]
    return {
        **base,
        "title": title,
        "description": description,
        "ogTitle": reader.og_title.strip()[:200],
        "ogDescription": reader.og_description.strip()[:400],
        "ogType": reader.og_type.strip()[:40],
        "ogImage": reader.og_image.strip()[:300],
        "siteName": reader.site_name.strip()[:120],
        "lang": reader.lang.strip()[:24],
        "images": reader.images[:8],
        "nav": reader.nav[:12],
        "faqs": faqs,
        "videos": reader.videos[:6],
        "reviewRating": schema.get("reviewRating") or "",
        "reviewCount": schema.get("reviewCount") or "",
        "h1": " | ".join(reader.h1)[:240],
        "headings": reader.headings[:14],
        "text": text[:4000],
        "words": len(text.split()),
        "renderHints": render_hints,
        "canonical": canonical,
        "canonicalElsewhere": bool(canonical) and page_key(canonical) != page_key(final),
        "noindex": reader.noindex,
        "contentHash": hashlib.sha256(
            f"{title}\n{description}\n{reader.og_title}\n{schema.get('schemaName')}\n{text}".encode()
        ).hexdigest()[:32],
        "links": reader.links[:400],
        # Heading outline, schema validation, accordion and image facts for the site audit.
        "audit": audit,
        "error": "",
        **schema,
        "faqs": faqs,
        "reviewRating": schema.get("reviewRating") or "",
        "reviewCount": schema.get("reviewCount") or "",
    }


def _sitemap_urls(client: httpx.Client, url: str, depth: int = 0, cap: int = 400, *, max_children: int = 12, timeout: float = 10.0) -> list[str]:
    if depth > 2:
        return []
    response = _get(client, url, timeout=timeout)
    if response is None:
        return []
    try:
        root = ET.fromstring(response.content[:3_000_000])
    except ET.ParseError:
        return []
    out: list[str] = []
    tag = root.tag.lower()
    locs = [node.text.strip() for node in root.iter() if node.tag.lower().endswith("loc") and node.text]
    if tag.endswith("sitemapindex"):
        preferred = sorted(locs, key=lambda loc: (0 if re.search(r"page|service|product|location|categor", loc, re.I) else 1, loc))
        for child in preferred[:max_children]:
            out.extend(_sitemap_urls(client, child, depth + 1, cap, max_children=max_children, timeout=timeout))
            if len(out) >= cap:
                break
        return out[:cap]
    return locs[:cap]


def _allowed(path_url: str, home_host: str) -> bool:
    parts = urlparse(path_url)
    if parts.scheme not in {"http", "https"}:
        return False
    if (parts.netloc or "").lower().removeprefix("www.") != home_host:
        return False
    if SKIP_EXT.search(parts.path or "") or SKIP_PATH.search(parts.path or ""):
        return False
    return True


def discover(home: str, limit: int, *, quick: bool = False) -> tuple[list[str], RobotFileParser | None]:
    home = normalize(home)
    home_host = host_of(home)
    robots = RobotFileParser()
    sitemaps: list[str] = []
    found: list[str] = []
    sitemap_timeout = 4.0 if quick else 10.0
    with httpx.Client(headers={"User-Agent": AGENT}) as client:
        robots_txt = _get(client, urljoin(home, "/robots.txt"), timeout=4.0 if quick else 6.0)
        if robots_txt is not None:
            lines = robots_txt.text.splitlines()
            robots.parse(lines)
            sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("sitemap:")]
        else:
            robots = None
        candidates = [*sitemaps, urljoin(home, "/sitemap.xml"), urljoin(home, "/sitemap_index.xml"), urljoin(home, "/wp-sitemap.xml")]
        if quick:
            candidates = candidates[:2]
        for candidate in candidates:
            urls = _sitemap_urls(
                client,
                candidate,
                cap=limit if quick else 400,
                max_children=1 if quick else 12,
                timeout=sitemap_timeout,
            )
            if urls:
                found.extend(urls)
                break

    seen: set[str] = set()
    ordered: list[str] = []
    for url in [home, *found]:
        clean = normalize(url)
        if clean in seen or not _allowed(clean, home_host):
            continue
        if robots is not None and not robots.can_fetch(AGENT, clean):
            continue
        seen.add(clean)
        ordered.append(clean)
    return ordered[: max(limit * 3, limit)], robots


def _priority(url: str) -> tuple[int, int]:
    path = urlparse(url).path.strip("/")
    if not path:
        return (0, 0)
    if LEGAL.search(path):
        return (4, len(path))
    if re.search(r"(blog|news|article|post|\d{4}/\d{2})", path, re.I):
        return (3, path.count("/"))
    return (1 if path.count("/") <= 1 else 2, path.count("/"))


def crawl(home: str, limit: int = 40, *, quick: bool = False) -> dict:
    """Read up to `limit` pages. Sitemap first, then internal links from the pages already read.

    ``quick`` is the SearchLites replay: a short page list, no sitemap-index walk, so the colour map
    can refresh without waiting on every URL on a large site.
    """
    home = normalize(home if home.startswith(("http://", "https://")) else f"https://{home}")
    if not public_host(home):
        return {"home": home, "pages": [], "error": "This address is not a public website."}
    home_host = host_of(home)
    page_limit = min(limit, 8) if quick else limit
    queue, robots = discover(home, page_limit, quick=quick)
    queue = sorted(queue, key=_priority)
    pages: list[dict] = []
    failed: list[dict] = []
    seen: set[str] = set()
    keys: set[str] = set()
    page_timeout = 6.0 if quick else 10.0
    max_rounds = 2 if quick else 4
    with httpx.Client(headers={"User-Agent": AGENT}) as client, ThreadPoolExecutor(max_workers=6) as pool:
        rounds = 0
        while queue and len(pages) < page_limit and rounds < max_rounds:
            rounds += 1
            batch = []
            for url in queue:
                if url not in seen and page_key(url) not in keys:
                    seen.add(url)
                    batch.append(url)
                if len(batch) >= page_limit - len(pages):
                    break
            queue = [url for url in queue if url not in seen]
            results = list(pool.map(lambda url: read_page(client, url, timeout=page_timeout), batch))
            fresh_links: list[str] = []
            for page in results:
                if page.get("error"):
                    if len(failed) < 50:
                        failed.append({"url": page.get("requestedUrl") or page.get("url"), "error": page["error"], "status": page.get("status")})
                    continue
                key = page_key(page["url"])
                if key in keys:
                    continue
                keys.add(key)
                for href in page.pop("links", []):
                    absolute = normalize(urljoin(page["url"], href.split("#", 1)[0]))
                    if absolute in seen or page_key(absolute) in keys or not _allowed(absolute, home_host):
                        continue
                    if robots is not None and not robots.can_fetch(AGENT, absolute):
                        continue
                    fresh_links.append(absolute)
                pages.append(page)
            queue = sorted(dict.fromkeys([*queue, *fresh_links]), key=_priority)
    for page in pages:
        page.pop("links", None)
    return {"home": home, "host": home_host, "pages": pages, "failed": failed, "error": "" if pages else "No readable pages were found."}


# 2: reads every offer + umbrella category, more copy per page, navigation.
# 3: best model (Astra), about twice the copy per page, failures are reported instead of hidden.
READER_VERSION = 3

UNDERSTAND = (
    "You are an SEO strategist reading a whole website before writing any Google listing. "
    "Use only what the pages and the owner's brief say. Do not invent awards, prices, years, reviews, or locations. "
    "Return JSON only, no prose, with this shape:\n"
    "Read EVERY page before deciding what the business is: the homepage, navigation, every service/product page, "
    "about and contact pages. A business that lists several services must be described by all of them, never by "
    "only the first one you read.\n"
    '{"business":"one sentence: who they are and the full range of what they sell",'
    '"category":"the umbrella term that covers ALL of the offers, in the words the site itself uses",'
    '"offers":["EVERY distinct service or product the site lists, merged where two names mean the same thing. Not a single case study, not the brand, and not the city by itself"],'
    '"audience":"who buys",'
    '"places":["cities or regions the site serves, only if stated"],'
    '"proof":["concrete facts on the site a listing may use, e.g. same-day service, free quote, 24/7"],'
    '"voice":"how the brand talks, 3-6 words",'
    '"brand":"the brand name as written on the site",'
    '"pages":[{"url":"...","role":"home|service|product|category|location|about|contact|blog|legal|other",'
    '"target":"the search a customer types for this page, 2-6 words. Homepage: the umbrella category that covers all offers, not one service, not the brand and not the city alone. Service page: that one service. Contact or about page: the page purpose with the brand (what a person searching for that page types), never a service. Include a city only when this page is about that place. Do not copy the current title.",'
    '"angle":"what makes this page different from the other pages on the site"}]}'
)


def _json_block(text: str) -> dict:
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def understand(pages: list[dict], brief: dict, profile: dict) -> dict:
    if not pages or not openai_configured():
        return {}
    digest = []
    for index, page in enumerate(pages[:45]):
        schema = ", ".join(page.get("schemaTypes") or []) or "-"
        # The homepage and the first pages carry the service list; give them more copy.
        copy_room = 3200 if index == 0 else 1400
        digest.append(
            f"URL: {page['url']}\nTITLE: {page.get('title') or '-'}\n"
            f"META DESCRIPTION: {page.get('description') or '-'}\n"
            f"OPEN GRAPH: {page.get('ogTitle') or '-'} — {page.get('ogDescription') or '-'}\n"
            f"JSON-LD: {schema}; name {page.get('schemaName') or '-'}\n"
            f"H1: {page.get('h1') or '-'}\n"
            f"NAVIGATION: {', '.join(page.get('nav') or [])[:300] or '-'}\n"
            f"HEADINGS: {' | '.join(page.get('headings') or [])[:900] or '-'}\n"
            f"COPY: {(page.get('text') or '')[:copy_room]}"
        )
    owner = "\n".join(
        f"{label}: {value}"
        for label, value in (
            ("What they sell", brief.get("businessType") or profile.get("services")),
            ("Business name", profile.get("business")),
            ("Where they serve", brief.get("market") or profile.get("areas") or profile.get("locations")),
            ("Reach", brief.get("reach")),
            ("90-day aim", brief.get("goal")),
            ("Never suggest", brief.get("avoid") or profile.get("restrictions")),
            ("Sensitive category", brief.get("sensitive")),
        )
        if value
    ) or "(The owner did not fill the setup questions.)"
    user = f"Owner's setup answers:\n{owner}\n\nPages on the site ({len(pages)} read):\n\n" + "\n\n".join(digest)
    try:
        # The business read decides the category, the offers and every keyword suggestion, so it
        # runs on the strongest model rather than the short-listing "seo" chain.
        text, model, _role = chat_text(
            system=UNDERSTAND,
            user=user[:120000],
            kind="analyst",
            writing_type="Site understanding",
            force_astra=True,
            max_tokens=12000,
        )
    except RuntimeError as exc:
        log.warning("site_understanding_failed", extra={"error": str(exc)[:200]})
        return {"_error": f"The AI read of the website failed: {str(exc)[:160]}"}
    data = _json_block(text)
    if not data:
        return {"_error": "The AI read of the website returned no usable answer."}
    data["_model"] = model
    return data


def _places_rivals(places_rows: list, own_host: str) -> list[dict]:
    rivals = []
    for row in places_rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        site = str(row[4] or "").strip()
        if not site.startswith("http") or host_of(site) == own_host:
            continue
        rivals.append({"title": str(row[0] or "")[:120], "url": site, "description": "", "source": "Google Places"})
    return rivals


def read_rival(client: httpx.Client, rival: dict) -> dict:
    url = rival.get("url") or ""
    if not url or not public_host(url):
        return rival
    page = read_page(client, url)
    if page.get("error"):
        return rival
    return {
        **rival,
        "title": page.get("title") or rival.get("title") or "",
        "description": page.get("description") or rival.get("description") or "",
        "h1": page.get("h1") or "",
        "headings": (page.get("headings") or [])[:6],
    }


def _manual_rivals(urls: list | str | None, own_host: str) -> list[dict]:
    if isinstance(urls, str):
        urls = re.split(r"[\s,]+", urls)
    out = []
    for raw in urls or []:
        value = str(raw or "").strip()
        if not value or "." not in value:
            continue
        url = value if value.startswith(("http://", "https://")) else f"https://{value}"
        if host_of(url) and host_of(url) != own_host:
            out.append({"title": host_of(url), "url": url, "description": "", "source": "Competitor you named"})
    return out[:6]


def research_competitors(
    targets: list[str],
    own_host: str,
    places_rows: list | None = None,
    per_query: int = 4,
    manual: list | str | None = None,
    market: str = "",
    serp_limit: int | None = None,
    counter: dict | None = None,
) -> dict:
    """Competitor listings per target search. Only real sources; an empty dict means none were available.
    serp_limit caps paid search-result lookups (None = no cap); counter["serp"] receives the number made."""
    from app import dataforseo
    from app.locations import LocationError, resolve

    try:
        place = resolve(market)
        location_name, language = place.country, place.language
    except LocationError:
        location_name, language = "", "en"
    out: dict[str, list[dict]] = {}
    named = _manual_rivals(manual, own_host)
    places = named or _places_rivals(places_rows or [], own_host)
    if named:
        with httpx.Client(headers={"User-Agent": AGENT}) as client, ThreadPoolExecutor(max_workers=6) as pool:
            places = list(pool.map(lambda rival: read_rival(client, rival), named))
    places_read = bool(named)
    with httpx.Client(headers={"User-Agent": AGENT}) as client, ThreadPoolExecutor(max_workers=6) as pool:
        for target in [t for t in dict.fromkeys(targets) if t][:8]:
            rivals: list[dict] = []
            made = (counter or {}).get("serp", 0)
            if dataforseo.configured() and location_name and (serp_limit is None or made < serp_limit):
                if counter is not None:
                    counter["serp"] = made + 1
                for row in dataforseo.competitor_serp(target, location_name=location_name, language_code=language):
                    if host_of(row.get("url") or "") == own_host:
                        continue
                    rivals.append({**row, "source": "Google results"})
                    if len(rivals) >= per_query:
                        break
            if rivals:
                out[target] = list(pool.map(lambda rival: read_rival(client, rival), rivals))
            elif places:
                if not named and not places_read:
                    places = list(pool.map(lambda rival: read_rival(client, rival), places[:per_query]))
                    places_read = True
                out[target] = places[:per_query]
    return out


def scan_site(
    home: str,
    *,
    brief: dict,
    profile: dict,
    places_rows: list | None = None,
    limit: int = 40,
    serp_limit: int | None = None,
    counter: dict | None = None,
    quick: bool = False,
) -> dict:
    crawled = crawl(home, limit=limit, quick=quick)
    pages = crawled.get("pages") or []
    # Replay only needs titles, headings and meta. The model read, competitor lookups and
    # AI-bot probes are what make "Replay the scan" sit at 100% for minutes.
    if quick:
        site_checks = {}
        ai_checks = {}
        understanding = {}
    else:
        try:
            from app.audit_checks import run_site_checks

            site_checks = run_site_checks(crawled.get("home") or home, [p["url"] for p in pages[:15]])
        except Exception:  # noqa: BLE001
            site_checks = {}
        try:
            from app.ai_readiness import run_ai_checks

            ai_checks = run_ai_checks(crawled.get("home") or home, [p["url"] for p in pages[:12]])
        except Exception:  # noqa: BLE001
            ai_checks = {}
        understanding = understand(pages, brief, profile)
    understand_error = str(understanding.pop("_error", "") or "")
    understand_model = str(understanding.pop("_model", "") or "")
    page_map = {normalize(item.get("url") or ""): item for item in understanding.get("pages") or [] if isinstance(item, dict)}
    targets = [
        str(page_map.get(page["url"], {}).get("target") or "").strip()
        for page in pages
        if page_map.get(page["url"], {}).get("role") not in {"legal", "contact", "about", "blog"}
    ]
    competitors = {}
    if not quick:
        competitors = research_competitors(
            [t for t in targets if t],
            crawled.get("host") or host_of(home),
            places_rows,
            manual=(brief or {}).get("competitors"),
            market=" ".join(str(v or "") for v in ((brief or {}).get("market"), profile.get("areas"), profile.get("locations"))),
            serp_limit=serp_limit,
            counter=counter,
        )
    return {
        "home": crawled.get("home"),
        "host": crawled.get("host"),
        "scannedAt": datetime.utcnow().isoformat(timespec="seconds"),
        "pages": pages,
        "business": {k: v for k, v in understanding.items() if k != "pages"},
        "understandModel": understand_model,
        # Shown to the owner: without a business read, keyword suggestions stay empty instead of guessing.
        "understandError": understand_error,
        # Bump when the site reader changes so stored scans are re-read once (operator._scan_fresh).
        "readerVersion": READER_VERSION,
        "pageMap": page_map,
        "competitors": competitors,
        "brief": brief,
        "failed": crawled.get("failed") or [],
        # robots.txt, sitemap and http/https/www redirect checks for the site audit.
        "siteChecks": site_checks,
        # AI crawler access (robots.txt per bot, firewall probe), llms.txt, nosnippet (AI search topic).
        "aiChecks": ai_checks,
        "error": crawled.get("error") or "",
    }


def _preview_tone(page: dict) -> tuple[str, str, str]:
    """Colour from what the page actually has. No invented scores."""
    title = str(page.get("title") or "").strip()
    description = str(page.get("description") or "").strip()
    words = int(page.get("words") or 0)
    if page.get("noindex"):
        return "red", "Hidden from Google", "This page asks Google not to index it."
    if not title:
        return "red", "Missing title", "Google has no title to show for this page."
    if words and words < 40:
        return "red", "Thin page", "There is almost no readable copy for Google to understand."
    if not description:
        return "yellow", "Missing description", "The search snippet will be chosen by Google."
    if len(title) < 12 or len(title) > 65:
        return "yellow", "Title length", "The title is hard to read in a search result."
    if not str(page.get("h1") or "").strip():
        return "yellow", "Missing heading", "The page has no H1."
    if page.get("canonicalElsewhere"):
        return "yellow", "Canonical elsewhere", "This URL points Google to a different page as the main one."
    return "green", "Listing is in place", "A title and description are on the page."


def _preview_platform(pages: list) -> str:
    blob = " ".join(
        f"{p.get('url') or ''} {p.get('text') or ''} {p.get('ogImage') or ''} "
        f"{' '.join((img.get('src') or '') for img in (p.get('images') or []) if isinstance(img, dict))}"
        for p in pages
    ).lower()
    if "wp-content" in blob or "wp-includes" in blob or "wordpress" in blob or "/wp-json" in blob:
        return "WordPress"
    if "cdn.shopify.com" in blob or "myshopify.com" in blob or "shopify" in blob:
        return "Shopify"
    if "webflow" in blob:
        return "Webflow"
    if "wix.com" in blob or "wixstatic" in blob:
        return "Wix"
    if "squarespace" in blob:
        return "Squarespace"
    return ""


def _topic_from_audit_title(title: str) -> str:
    label_l = str(title or "").lower()
    if "title" in label_l and "contentful" not in label_l:
        return "title"
    if "description" in label_l:
        return "description"
    if "heading" in label_l or "h1" in label_l:
        return "heading"
    if any(k in label_l for k in ("image", "img", "webp", "avif", "offscreen", "responsive image", "srcset")):
        return "image"
    if any(k in label_l for k in ("server response", "ttfb", "time to first byte", "document request")):
        return "ssr"
    if any(k in label_l for k in ("render-blocking", "render blocking", "critical request")):
        return "ssr"
    if "css" in label_l or "stylesheet" in label_l:
        return "css"
    if any(k in label_l for k in ("javascript", "boot-up", "bootup", "main-thread", "main thread", "js ")):
        return "csr" if any(k in label_l for k in ("unused javascript", "boot", "main-thread", "main thread")) else "javascript"
    if "lcp" in label_l or "largest contentful" in label_l:
        return "lcp"
    if "cls" in label_l or "layout shift" in label_l:
        return "cls"
    return "speed"


def _preview_markup_quality(pages: list, html_slice: str = "") -> dict:
    """Title/description/heading typography, BEM class hygiene, CSS/JS footprint from live HTML."""
    home = pages[0] if pages else {}
    hints = home.get("renderHints") if isinstance(home.get("renderHints"), dict) else {}
    issues: list[dict] = []

    def push(topic: str, color: str, label: str, detail: str) -> None:
        issues.append(
            {
                "topic": topic,
                "color": color,
                "path": "/",
                "label": label,
                "detail": detail,
                "title": str(home.get("title") or ""),
                "source": "markup",
            }
        )

    # Heading / typography
    h1 = str(home.get("h1") or "").strip()
    headings = home.get("headings") if isinstance(home.get("headings"), list) else []
    h1_parts = [p.strip() for p in h1.split("|") if p.strip()] if h1 else []
    if not h1:
        push("typography", "yellow", "Missing H1 heading", "The homepage has no H1. Typography hierarchy starts with one clear H1.")
    elif len(h1_parts) > 1:
        push(
            "typography",
            "yellow",
            "Multiple H1-style headings on the homepage",
            "Google prefers one clear H1. Extra H1s weaken typography and topic focus.",
        )
    if headings and len(headings) >= 2:
        # Skipped levels are hard without tag names; flag very flat outlines.
        if len(headings) <= 2 and int(home.get("words") or 0) > 200:
            push(
                "heading",
                "yellow",
                "Thin heading outline",
                "Few H2/H3 headings for a long page — structure the copy with clearer typography.",
            )

    # BEM / class naming hygiene from raw HTML or a classSample token string
    sample = html_slice or ""
    if not sample and isinstance(hints.get("classSample"), str):
        sample = hints["classSample"]
    classes = re.findall(r'class=["\']([^"\']+)["\']', sample[:200_000], flags=re.I)
    tokens: list[str] = []
    for block in classes[:400]:
        tokens.extend(t for t in re.split(r"\s+", block) if t)
    if not tokens and sample and "class=" not in sample.lower():
        tokens = [t for t in re.split(r"\s+", sample) if t and len(t) < 80][:400]
    bem_like = 0
    if tokens:
        bem_like = sum(1 for t in tokens if "__" in t or "--" in t or re.match(r"^[a-z]+(-[a-z0-9]+)+$", t))
        utility_heavy = sum(1 for t in tokens if re.match(r"^(p|m|mt|mb|px|py|w|h|text|bg|flex|grid)-", t))
        chaotic = sum(1 for t in tokens if re.search(r"[A-Z].*[A-Z]|_|[0-9]{3,}", t) and "__" not in t)
        ratio = bem_like / max(1, len(tokens))
        if ratio < 0.08 and chaotic > 12:
            push(
                "bem",
                "yellow",
                "CSS class names look inconsistent (not BEM-like)",
                "Mixed, hard-to-maintain class names make CSS refactors risky. Prefer Block__Element--Modifier (BEM) or a clear system.",
            )
        if utility_heavy > len(tokens) * 0.55:
            push(
                "css",
                "yellow",
                "Utility-class CSS dominates the markup",
                "Heavy utility CSS can bloat HTML and hurt maintainability. Audit unused CSS with PageSpeed.",
            )

    scripts = int(hints.get("scriptTags") or 0)
    if scripts >= 20:
        push(
            "javascript",
            "yellow",
            f"{scripts} script tags on the homepage",
            "A large JavaScript footprint often slows LCP and INP. Prefer SSR for critical content and defer non-critical JS.",
        )
    imgs = int(hints.get("imgTags") or 0)
    sized = int(hints.get("imgSized") or 0)
    if imgs and sized < max(1, imgs // 2):
        push(
            "image",
            "yellow",
            "Images missing width/height attributes",
            "Unset image dimensions cause layout shift (CLS). Set sizes in SSR HTML.",
        )

    return {"issues": issues[:10], "classTokens": len(tokens), "bemRatio": round((bem_like / max(1, len(tokens))), 3) if tokens else 0}


def _preview_rendering(pages: list, pagespeed: dict | None = None) -> dict:
    """Detect SSR vs CSR delivery and server-side fixes for images / loading speed."""
    home = pages[0] if pages else {}
    hints = home.get("renderHints") if isinstance(home.get("renderHints"), dict) else {}
    words = int(home.get("words") or 0)
    scripts = int(hints.get("scriptTags") or 0)
    imgs = int(hints.get("imgTags") or 0)
    sized = int(hints.get("imgSized") or 0)
    lazy = int(hints.get("imgLazy") or 0)
    srcset = int(hints.get("imgSrcset") or 0)
    spa = bool(
        hints.get("nextData")
        or hints.get("nextRoot")
        or hints.get("reactRoot")
        or hints.get("vue")
        or hints.get("angular")
    )
    # Thin initial HTML + lots of scripts ⇒ content likely waits on client JS (CSR).
    csr_heavy = bool(spa and words < 80 and scripts >= 8) or (words < 40 and scripts >= 12)
    ssr_like = words >= 120 and (not spa or hints.get("nextData"))
    if csr_heavy:
        mode = "csr"
        mode_label = "Client-side rendered (CSR)"
        mode_note = "Most of the readable copy arrives after JavaScript runs in the browser."
    elif hints.get("nextData") or ssr_like:
        mode = "ssr"
        mode_label = "Server-side rendered (SSR)"
        mode_note = "HTML for this page is largely ready from the server — keep images and LCP work on the server path too."
    elif spa:
        mode = "hybrid"
        mode_label = "Hybrid / app shell"
        mode_note = "An app framework is present; some content may still wait on client JavaScript."
    else:
        mode = "ssr"
        mode_label = "Server HTML"
        mode_note = "Page copy is present in the initial HTML response."

    issues: list[dict] = []
    solutions: list[dict] = []

    def push_issue(topic: str, color: str, label: str, detail: str) -> None:
        issues.append(
            {
                "topic": topic,
                "color": color,
                "path": "/",
                "label": label,
                "detail": detail,
                "title": "",
                "source": "render",
            }
        )

    def push_solution(topic: str, label: str, detail: str) -> None:
        solutions.append({"topic": topic, "label": label, "detail": detail, "kind": "ssr"})

    if csr_heavy:
        push_issue(
            "csr",
            "red",
            "Important content waits on client-side JavaScript",
            "Google and visitors see a thin shell first. Ship the main copy and hero with SSR/SSG so text and images arrive in the first HTML.",
        )
        push_solution(
            "ssr",
            "Render the homepage on the server (SSR/SSG)",
            "Serve title, headings, and above-the-fold copy in the first HTML response instead of waiting for CSR hydration.",
        )
        push_solution(
            "image",
            "SSR the hero image with width, height, and modern formats",
            "Preload the LCP image from the server HTML (WebP/AVIF, srcset, explicit dimensions) so loading speed does not wait on client JS.",
        )

    if imgs and sized < max(1, imgs // 2):
        push_issue(
            "image",
            "yellow",
            "Images missing width/height in the HTML",
            f"{imgs - sized} of {imgs} images on the home HTML lack size attributes — this often hurts CLS and LCP.",
        )
        push_solution(
            "ssr",
            "Set image dimensions in server-rendered HTML",
            "Add width/height (or aspect-ratio) on SSR markup so the browser reserves space before CSS/JS load.",
        )

    if imgs and srcset == 0:
        push_issue(
            "image",
            "yellow",
            "No responsive image srcset in the initial HTML",
            "The server HTML does not advertise multiple image sizes. Mobile may download a desktop hero.",
        )
        push_solution(
            "ssr",
            "Emit responsive images from the server",
            "SSR srcset/sizes (and modern formats) so loading speed is decided on the server, not after CSR.",
        )

    if imgs and lazy == 0 and imgs >= 4:
        push_solution(
            "image",
            "Lazy-load below-the-fold images in SSR markup",
            "Keep the LCP/hero eager in server HTML; mark the rest loading=\"lazy\" so first paint stays fast.",
        )

    vitals = (pagespeed or {}).get("vitals") or {}
    ratings = (pagespeed or {}).get("ratings") or {}
    ttfb = str(vitals.get("ttfb") or "")
    if str(ratings.get("lcp") or "").lower() in {"poor", "needs improvement"}:
        push_solution(
            "ssr",
            "Fix LCP on the server path",
            f"LCP is {vitals.get('lcp') or 'slow'}. Prioritize SSR for the LCP element (usually the hero image or H1 block) and shrink server TTFB.",
        )
    if ttfb and ttfb not in {"—", "-", ""}:
        # Heuristic: if TTFB display looks like multi-second, flag SSR hosting.
        push_solution(
            "ssr",
            "Improve server response time (TTFB)",
            f"PageSpeed reports TTFB {ttfb}. Cache SSR HTML at the edge and compress images on the server before they reach the browser.",
        )

    # Fold PageSpeed issues that already map to CSR/SSR/image.
    for row in (pagespeed or {}).get("issues") or []:
        topic = str(row.get("topic") or "")
        if topic in {"csr", "ssr", "image"} and not any(i["label"] == row.get("label") for i in issues):
            issues.append({**row, "source": row.get("source") or "pagespeed"})

    return {
        "mode": mode,
        "modeLabel": mode_label,
        "modeNote": mode_note,
        "csrHeavy": csr_heavy,
        "spaSignals": {
            "next": bool(hints.get("nextData") or hints.get("nextRoot")),
            "react": bool(hints.get("reactRoot")),
            "vue": bool(hints.get("vue")),
            "angular": bool(hints.get("angular")),
        },
        "stats": {
            "words": words,
            "scripts": scripts,
            "images": imgs,
            "imagesSized": sized,
            "imagesLazy": lazy,
            "imagesSrcset": srcset,
        },
        "issues": issues[:8],
        "solutions": solutions[:6],
    }


def _preview_pagespeed(home: str) -> dict:
    """Optional PageSpeed Insights pass for the public free analysis (LCP/CLS/title/CSS/JS/SSR)."""
    try:
        from app import config
        from app.google_oauth import run_pagespeed
    except Exception:  # noqa: BLE001
        return {}
    if not getattr(config, "GOOGLE_PAGESPEED_API_KEY", ""):
        return {}
    # One strategy at a time. Two PageSpeed calls at once get dropped, and a dropped mobile
    # call used to wipe the whole speed report.
    try:
        mobile = run_pagespeed(home, "mobile")
    except Exception as exc:  # noqa: BLE001
        log.warning("preview_pagespeed_failed", extra={"error": str(exc)[:180]})
        return {}
    try:
        desktop = run_pagespeed(home, "desktop")
    except Exception as exc:  # noqa: BLE001
        log.warning("preview_pagespeed_desktop_failed", extra={"error": str(exc)[:180]})
        desktop = {}

    vitals = mobile.get("vitals") or {}
    ratings = mobile.get("vitalRatings") or {}
    scores = mobile.get("scores") or {}
    issues: list[dict] = []

    def push(topic: str, color: str, label: str, detail: str) -> None:
        issues.append(
            {
                "topic": topic,
                "color": color,
                "path": "/",
                "label": label,
                "detail": detail,
                "title": "",
                "source": "pagespeed",
            }
        )

    # Core Web Vitals
    for key, topic, human in (
        ("LCP", "lcp", "Largest Contentful Paint"),
        ("CLS", "cls", "Cumulative Layout Shift"),
        ("INP", "inp", "Interaction to Next Paint"),
    ):
        rating = str(ratings.get(key) or "").lower()
        value = vitals.get(key) or "—"
        if rating == "poor":
            push(topic, "red", f"{human} is poor ({value})", f"Google PageSpeed marks {human} as poor on mobile.")
        elif rating == "needs improvement":
            push(
                topic,
                "yellow",
                f"{human} needs work ({value})",
                f"Google PageSpeed marks {human} as needs improvement on mobile.",
            )

    # Prefer failing SEO + performance audits from the structured rows.
    for row in (mobile.get("seoAudits") or []) + (mobile.get("perfAudits") or []):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        title = str(row[0] or "")
        status = str(row[1] or "")
        value = str(row[2] or "") if len(row) > 2 else ""
        detail = str(row[4] or "") if len(row) > 4 else ""
        if status not in {"Fail", "Average"}:
            continue
        topic = _topic_from_audit_title(title)
        color = "red" if status == "Fail" else "yellow"
        push(topic, color, title, (detail or value or f"PageSpeed status: {status}")[:220])
        if len(issues) >= 14:
            break

    # Opportunities often cover unused CSS/JS / images even when score rows are sparse.
    for row in mobile.get("opportunities") or []:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        title = str(row[0] or "")
        lower = title.lower()
        topic = _topic_from_audit_title(title)
        if "unused css" in lower:
            push("css", "yellow", title, str(row[1] or row[2] or "Reduce unused CSS.")[:220])
        elif "unused javascript" in lower or "unused js" in lower:
            push("csr", "yellow", title, str(row[1] or row[2] or "Reduce unused client JavaScript (CSR weight).")[:220])
        elif topic in {"image", "ssr", "lcp", "csr"}:
            push(topic, "yellow", title, str(row[1] or row[2] or "")[:220])
        elif "largest contentful paint" in lower:
            push("lcp", "yellow", title, str(row[1] or row[2] or "")[:220])
        if len(issues) >= 18:
            break

    # Deduplicate by label
    seen: set[str] = set()
    unique: list[dict] = []
    for item in issues:
        key = str(item.get("label") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return {
        "source": "google_pagespeed",
        "strategy": "mobile",
        "scores": scores,
        "vitals": {
            "lcp": vitals.get("LCP"),
            "cls": vitals.get("CLS"),
            "inp": vitals.get("INP"),
            "fcp": vitals.get("FCP"),
            "tbt": vitals.get("TBT"),
            "si": vitals.get("SI"),
            "ttfb": vitals.get("TTFB"),
        },
        "ratings": {
            "lcp": ratings.get("LCP"),
            "cls": ratings.get("CLS"),
            "inp": ratings.get("INP"),
            "fcp": ratings.get("FCP"),
        },
        "issues": unique[:12],
        "performance": scores.get("performance"),
        "seo": scores.get("seo"),
        "desktop": {
            "performance": (desktop.get("scores") or {}).get("performance"),
            "seo": (desktop.get("scores") or {}).get("seo"),
            "lcp": (desktop.get("vitals") or {}).get("LCP"),
            "cls": (desktop.get("vitals") or {}).get("CLS"),
        },
    }


def _grade_from_score(score: float) -> tuple[str, str]:
    if score >= 92:
        return "A+", "#3DDC84"
    if score >= 88:
        return "A", "#3DDC84"
    if score >= 80:
        return "B+", "#3DDC84"
    if score >= 70:
        return "B", "#FFC53D"
    if score >= 55:
        return "C+", "#FFC53D"
    if score >= 40:
        return "C", "#FFC53D"
    return "D", "#FF5A4E"


def _position_color(position: int | None) -> str:
    if not position or position < 1:
        return "yellow"
    if position <= 10:
        return "green"
    if position <= 20:
        return "yellow"
    return "red"


def _as_score(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 100:
        return None
    return number


def _google_health(*, page_rows: list, ranked: list, onpage: dict, pagespeed: dict) -> dict:
    """Letter grade and heatmap from measured scores and live rankings. No filler dots."""
    dots: list[dict] = []
    kind = "pages"
    if ranked:
        kind = "rankings"
        for row in ranked[:48]:
            position = row.get("position")
            dots.append(
                {
                    "color": _position_color(position if isinstance(position, int) else None),
                    "path": str(row.get("page") or row.get("url") or ""),
                    "label": str(row.get("keyword") or ""),
                    "position": position,
                }
            )
    else:
        for row in page_rows[:48]:
            dots.append(
                {
                    "color": str(row.get("color") or "yellow"),
                    "path": str(row.get("path") or "/"),
                    "label": str(row.get("label") or row.get("path") or ""),
                }
            )
    counts = {"red": 0, "yellow": 0, "green": 0, "blue": 0}
    for dot in dots:
        color = dot["color"] if dot["color"] in counts else "yellow"
        dot["color"] = color
        counts[color] += 1

    weighted: list[tuple[float, float]] = []
    onpage_score = _as_score((onpage or {}).get("score"))
    if onpage_score is not None:
        weighted.append((onpage_score, 0.4))
    seo_score = _as_score((pagespeed or {}).get("seo"))
    if seo_score is not None:
        weighted.append((seo_score, 0.2))
    if ranked:
        points = []
        for row in ranked:
            position = row.get("position")
            try:
                position = int(position) if position is not None else 100
            except (TypeError, ValueError):
                position = 100
            points.append(100 if position <= 10 else 55 if position <= 20 else 15)
        weighted.append((sum(points) / len(points), 0.4))
    score = None
    if weighted:
        weight = sum(item[1] for item in weighted)
        score = sum(item[0] * item[1] for item in weighted) / weight
        grade, color = _grade_from_score(score)
    else:
        grade, color, _note = _preview_grade(counts)

    page_one = sum(1 for row in ranked if isinstance(row.get("position"), int) and row["position"] <= 10)
    if ranked:
        note = f"{page_one} of {len(ranked)} searches on page one"
    elif dots:
        note = f"{counts['green']} of {len(dots)} pages listing-ready"
    else:
        note = "no pages read yet"
    detail_bits = []
    if onpage_score is not None:
        detail_bits.append(f"On-page {onpage_score:g}")
    if seo_score is not None:
        detail_bits.append(f"PageSpeed SEO {int(seo_score)}")
    perf = _as_score((pagespeed or {}).get("performance"))
    if perf is not None:
        detail_bits.append(f"mobile speed {int(perf)}")
    return {
        "grade": grade,
        "gradeColor": color,
        "gradeNote": note,
        "gradeDetail": " · ".join(detail_bits),
        "score": round(score, 1) if score is not None else None,
        "dots": dots,
        "counts": counts,
        "kind": kind,
    }


def _preview_grade(counts: dict) -> tuple[str, str, str]:
    """Letter grade from page tones only — never invents keyword ranks."""
    red = int(counts.get("red") or 0)
    yellow = int(counts.get("yellow") or 0)
    green = int(counts.get("green") or 0)
    total = max(1, red + yellow + green)
    score = (green * 100 + yellow * 55 + red * 15) / total
    if score >= 92:
        return "A+", "#3DDC84", "excellent listing health"
    if score >= 88:
        return "A", "#3DDC84", "strong listing health"
    if score >= 80:
        return "B+", "#3DDC84", "mostly healthy"
    if score >= 70:
        return "B", "#FFC53D", "good, with gaps"
    if score >= 55:
        return "C+", "#FFC53D", "fair, with quick wins"
    if score >= 40:
        return "C", "#FFC53D", "needs work"
    return "D", "#FF5A4E", "urgent fixes first"


_CATEGORY_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("web design agency", ("web design", "website design", "web development", "wordpress agency", "web agency")),
    ("digital marketing agency", ("digital marketing", "seo agency", "marketing agency", "ppc agency", "content marketing")),
    ("software company", ("saas", "software company", "software development", "app development", "product studio")),
    ("roofing contractor", ("roofing", "roof repair", "roof replacement", "roofer")),
    ("plumbing company", ("plumber", "plumbing", "drain cleaning")),
    ("hvac company", ("hvac", "air conditioning", "furnace", "heating and cooling")),
    ("dentist", ("dentist", "dental clinic", "orthodont")),
    ("law firm", ("law firm", "attorney", "lawyer", "legal services")),
    ("real estate agency", ("real estate", "realtor", "property management")),
    ("restaurant", ("restaurant", "cafe", "bistro", "dining")),
    ("fitness gym", ("gym", "fitness", "personal trainer")),
    ("auto repair", ("auto repair", "mechanic", "car service")),
]


_CITY_HINTS = (
    "calgary", "edmonton", "toronto", "vancouver", "ottawa", "montreal", "winnipeg", "halifax",
    "london", "manchester", "birmingham", "glasgow", "edinburgh",
    "sydney", "melbourne", "brisbane", "perth", "auckland",
    "new york", "los angeles", "chicago", "houston", "phoenix", "philadelphia", "san antonio",
    "san diego", "dallas", "san jose", "austin", "jacksonville", "fort worth", "columbus",
    "charlotte", "indianapolis", "san francisco", "seattle", "denver", "boston", "nashville",
    "detroit", "portland", "las vegas", "miami", "atlanta", "minneapolis",
)


def _page_blob(pages: list, limit: int = 12) -> str:
    parts = []
    for page in pages[:limit]:
        parts.append(str(page.get("title") or ""))
        parts.append(str(page.get("description") or ""))
        parts.append(str(page.get("h1") or ""))
        parts.append(" ".join(str(h) for h in (page.get("headings") or [])[:8]))
        parts.append(" ".join(str(n) for n in (page.get("nav") or [])[:12]))
        parts.append(str(page.get("schemaName") or ""))
        parts.append(" ".join(str(t) for t in (page.get("schemaTypes") or [])[:6]))
        parts.append(str(page.get("text") or "")[:900])
        parts.append(urlparse(page.get("url") or "").path or "")
    return " ".join(parts).lower()


def _preview_city(pages: list) -> str:
    blob = _page_blob(pages)
    for city in _CITY_HINTS:
        if re.search(rf"\b{re.escape(city)}\b", blob):
            return city.title()
    try:
        from app import locations as loc

        for key, (_label, _country) in loc.REGIONS.items():
            if re.search(rf"\b{re.escape(key)}\b", blob):
                return key.title()
    except Exception:
        pass
    return ""


def _preview_category(pages: list, brand: str = "") -> str:
    """Infer the business category from schema, titles, nav and copy — not the brand alone."""
    blob = _page_blob(pages)
    schema_types = " ".join(
        str(t).lower() for page in pages for t in (page.get("schemaTypes") or []) if t
    )
    schema_name = " ".join(str(page.get("schemaName") or "") for page in pages[:4]).strip()
    scored: list[tuple[int, str]] = []
    for label, needles in _CATEGORY_HINTS:
        hits = sum(1 for needle in needles if needle in blob)
        if hits:
            scored.append((hits, label))
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][1]
    if "localbusiness" in schema_types or "professionalservice" in schema_types:
        if schema_name and brand and schema_name.lower() != brand.lower():
            return schema_name[:60]
        return "local business"
    # Fall back to a concrete homepage H1 / nav phrase (skip pure brand).
    home = next((p for p in pages if (urlparse(p.get("url") or "").path or "/") in {"", "/"}), pages[0] if pages else None)
    for candidate in (
        str((home or {}).get("h1") or "").split("|")[0].strip(),
        *((home or {}).get("nav") or [])[:6],
        str((home or {}).get("title") or "").split("|")[0].strip(),
    ):
        text = " ".join(str(candidate or "").split())
        if len(text) < 4:
            continue
        if brand and text.lower() == brand.lower():
            continue
        if text.lower() in {"home", "welcome", "menu"}:
            continue
        return text[:60]
    return "local business"


def _preview_places(host: str, brand_hint: str = "", category: str = "", city: str = "") -> list[dict]:
    """Google Places rivals by category + city. Never search brand-only “near me” (wrong vertical)."""
    try:
        from app import config
        from app import google_oauth as goauth
    except Exception:
        return []
    if not getattr(config, "GOOGLE_PLACES_API_KEY", ""):
        return []
    brand = (brand_hint or host or "").split(".")[0].replace("-", " ").strip()
    category = _usable_category(category)
    city = (city or "").strip()
    # A Places search with no city is biased to wherever the request comes from,
    # so a German tire site was matched to web studios in Edmonton.
    if not category or not city:
        return []
    queries = []
    if city:
        queries.append(f"{category} in {city}")
        queries.append(f"best {category} {city}")
    else:
        queries.append(category)
    # Own listing lookup last — useful when the brand has a Google profile, not for rivals.
    if brand and len(brand) >= 2:
        queries.append(f"{brand} {city}".strip() if city else brand)

    out: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        try:
            rows = goauth.places_text_search(query, max_results=8)
        except Exception:
            continue
        for row in rows or []:
            name = row[0] if len(row) > 0 else ""
            address = row[1] if len(row) > 1 else ""
            website = row[4] if len(row) > 4 else ""
            key = (name or "").strip().lower()
            if not key or key in seen:
                continue
            site_host = host_of(website) if website else ""
            # Skip the scanned site itself from the rival list.
            if site_host and site_host == host:
                continue
            # Brand-only query can return the owner's listing — keep others for competitors.
            if query.startswith(brand) and brand.lower() in key and site_host == host:
                continue
            seen.add(key)
            out.append(
                {
                    "name": name,
                    "address": address,
                    "rating": row[2] if len(row) > 2 else "—",
                    "reviews": row[3] if len(row) > 3 else "0",
                    "website": website,
                    "query": query,
                    "category": category,
                    "city": city,
                }
            )
            if len(out) >= 6:
                break
        if len(out) >= 4:
            break
    return out[:6]


def _ctr_share(position: int | None) -> float:
    """Public CTR curve used only to estimate visits from ranked positions × volume."""
    if not position or position < 1:
        return 0.0
    table = {
        1: 0.28,
        2: 0.15,
        3: 0.11,
        4: 0.08,
        5: 0.06,
        6: 0.05,
        7: 0.04,
        8: 0.03,
        9: 0.025,
        10: 0.02,
    }
    if position <= 10:
        return table.get(position, 0.02)
    if position <= 20:
        return 0.01
    if position <= 30:
        return 0.004
    return 0.002


def _rank_status(position: int | None) -> tuple[str, str]:
    if not position or position < 1:
        return "Unknown", "yellow"
    if position <= 10:
        return "Page one", "green"
    page = (position - 1) // 10 + 1
    if position <= 20:
        return f"Near miss · page {page}", "yellow"
    return f"Buried · page {page}", "red"


# Country-code TLD → DataForSEO location + language. Used only when the pages do not name a country.
_TLD_MARKET = {
    "de": ("Germany", "de"),
    "at": ("Austria", "de"),
    "ch": ("Switzerland", "de"),
    "fr": ("France", "fr"),
    "es": ("Spain", "es"),
    "it": ("Italy", "it"),
    "nl": ("Netherlands", "nl"),
    "be": ("Belgium", "nl"),
    "pt": ("Portugal", "pt"),
    "br": ("Brazil", "pt"),
    "mx": ("Mexico", "es"),
    "uk": ("United Kingdom", "en"),
    "ie": ("Ireland", "en"),
    "ca": ("Canada", "en"),
    "au": ("Australia", "en"),
    "nz": ("New Zealand", "en"),
    "in": ("India", "en"),
    "za": ("South Africa", "en"),
    "ae": ("United Arab Emirates", "en"),
    "sg": ("Singapore", "en"),
}
_LANG_MARKET = {
    "de": ("Germany", "de"),
    "fr": ("France", "fr"),
    "es": ("Spain", "es"),
    "it": ("Italy", "it"),
    "nl": ("Netherlands", "nl"),
    "pt": ("Portugal", "pt"),
    "en": ("United States", "en"),
}
_DE_CITIES = (
    "berlin", "munich", "münchen", "hamburg", "cologne", "köln", "frankfurt", "stuttgart",
    "düsseldorf", "dusseldorf", "leipzig", "dortmund", "essen", "bremen", "dresden",
    "hannover", "nuremberg", "nürnberg", "duisburg", "bochum", "wuppertal", "bielefeld",
    "bonn", "münster", "karlsruhe", "mannheim", "augsburg", "wiesbaden", "freiburg",
)


def _market_from_text(blob: str) -> tuple[str, str] | None:
    """Country named on the site. None when the copy does not say."""
    text = (blob or "").lower()
    if re.search(r"\b(deutschland|bundesrepublik)\b", text):
        return "Germany", "de"
    if re.search(r"\b(österreich|oesterreich)\b", text):
        return "Austria", "de"
    if re.search(r"\b(schweiz|switzerland)\b", text):
        return "Switzerland", "de"
    try:
        from app import locations as loc

        for key, (country, _iso, lang) in loc.COUNTRIES.items():
            if re.search(rf"\b{re.escape(key)}\b", text):
                return country, lang
        for key, (_label, country_key) in loc.REGIONS.items():
            if re.search(rf"\b{re.escape(key)}\b", text):
                country, _iso, lang = loc.COUNTRIES.get(country_key, ("", "", "en"))
                if country:
                    return country, lang
        for key, country_key in loc.CITIES.items():
            if key in loc.AMBIGUOUS_CITIES:
                continue
            if re.search(rf"\b{re.escape(key)}\b", text):
                country, _iso, lang = loc.COUNTRIES.get(country_key, ("", "", "en"))
                if country:
                    return country, lang
    except Exception:
        pass
    return None


def _page_lang(pages: list) -> str:
    for page in pages[:4]:
        raw = str(page.get("lang") or "").lower().replace("_", "-").strip()
        if raw:
            return raw.split("-", 1)[0]
    return ""


def _detect_market(home: str, pages: list) -> tuple[str, str, str]:
    """Country and language for search data. Source is tld, page copy, or html lang — never a silent US guess for a local TLD."""
    blob = _page_blob(pages)
    named = _market_from_text(blob)
    host = host_of(home)
    tld = "uk" if host.endswith(".uk") else (host.rsplit(".", 1)[-1] if "." in host else "")
    tld_market = _TLD_MARKET.get(tld)
    lang = _page_lang(pages)
    if named and not tld_market:
        return (*named, "page")
    if tld_market:
        country, language = tld_market
        if tld == "ch" and lang in {"de", "fr", "it", "en"}:
            language = lang
        elif lang in {"de", "fr", "es", "it", "nl", "pt", "en"} and tld not in {"ch"}:
            # Keep the country from the domain. Use the page language when it is a real language code.
            if lang != "en" or language == "en":
                language = lang if lang else language
        return country, language, "tld"
    if named:
        return (*named, "page")
    if lang in _LANG_MARKET:
        country, language = _LANG_MARKET[lang]
        return country, language, "lang"
    return "United States", "en", "default"


def _detect_city(pages: list, country: str) -> str:
    for page in pages[:6]:
        city = str(page.get("schemaCity") or "").strip()
        if city:
            return city
    blob = _page_blob(pages)
    if country == "Germany":
        for city in _DE_CITIES:
            if re.search(rf"\b{re.escape(city)}\b", blob):
                return city.title() if city not in {"münchen", "köln", "düsseldorf", "nürnberg"} else {
                    "münchen": "München",
                    "köln": "Köln",
                    "düsseldorf": "Düsseldorf",
                    "nürnberg": "Nürnberg",
                }[city]
    return _preview_city(pages)


def _preview_visibility(host: str, pages: list, places: list, *, location: str = "", language: str = "") -> dict:
    """Real ranked keywords + opportunity ideas when Searchify SEO (DataForSEO) is connected.

    Empty buckets stay empty — nothing is invented for the free report.
    """
    empty = {
        "keywords": [],
        "gaps": [],
        "visibility": {
            "searches": 0,
            "pageOne": 0,
            "nearMisses": 0,
            "visits": 0,
            "market": "",
            "source": "",
        },
        "researchSource": "",
    }
    try:
        from app import research
    except Exception:
        return empty
    if not research.configured():
        return empty
    if not location:
        location, language, _source = _detect_market(host if "://" in host else f"https://{host}", pages)
    ranked: list[dict] = []
    gaps: list[dict] = []
    try:
        ranked_res, _ = research._call(
            "POST",
            "/dataforseo_labs/google/ranked_keywords/live",
            [
                {
                    "target": host,
                    "location_name": location,
                    "language_code": language,
                    "limit": 50,
                    "item_types": ["organic"],
                    "filters": ["ranked_serp_element.serp_item.rank_group", "<=", 100],
                    "order_by": [
                        "ranked_serp_element.serp_item.rank_group,asc",
                        "keyword_data.keyword_info.search_volume,desc",
                    ],
                }
            ],
            timeout=45.0,
        )
    except Exception:
        try:
            ranked_res, _ = research._call(
                "POST",
                "/dataforseo_labs/google/ranked_keywords/live",
                [
                    {
                        "target": host,
                        "location_name": location,
                        "language_code": language,
                        "limit": 50,
                        "order_by": ["keyword_data.keyword_info.search_volume,desc"],
                    }
                ],
                timeout=45.0,
            )
        except Exception:
            ranked_res = []
    best: dict[str, dict] = {}
    for item in ((ranked_res or [{}])[0] or {}).get("items") or []:
        row = research._kw_row(item)
        serp = (item.get("ranked_serp_element") or {}).get("serp_item") or {}
        if (serp.get("type") or "organic") not in ("", "organic"):
            continue
        keyword = str(row.get("keyword") or "").strip()
        if not keyword:
            continue
        position = serp.get("rank_group") or serp.get("rank_absolute")
        try:
            position = int(position) if position is not None else None
        except (TypeError, ValueError):
            position = None
        volume = row.get("volume")
        try:
            volume = int(volume) if volume is not None else None
        except (TypeError, ValueError):
            volume = None
        status, tone = _rank_status(position)
        candidate = {
            "keyword": keyword,
            "volume": volume,
            "position": position,
            "status": status,
            "tone": tone,
            "url": str(serp.get("url") or "")[:300],
            "page": str(serp.get("relative_url") or "")[:200],
        }
        key = keyword.lower()
        held = best.get(key)
        if held is None or (position or 10**6) < (held.get("position") or 10**6):
            best[key] = candidate
    ranked = sorted(
        best.values(),
        key=lambda row: (
            row.get("position") or 10**6,
            -(row.get("volume") or 0),
            row.get("keyword") or "",
        ),
    )
    known = {row["keyword"].lower() for row in ranked}
    try:
        site_res, _ = research._call(
            "POST",
            "/dataforseo_labs/google/keywords_for_site/live",
            [
                {
                    "target": host,
                    "location_name": location,
                    "language_code": language,
                    "include_subdomains": True,
                    "limit": 40,
                    "filters": ["keyword_info.search_volume", ">", 0],
                    "order_by": ["relevance,desc"],
                }
            ],
            timeout=45.0,
        )
    except Exception:
        try:
            site_res, _ = research._call(
                "POST",
                "/dataforseo_labs/google/keywords_for_site/live",
                [{"target": host, "location_name": location, "language_code": language, "limit": 40}],
                timeout=45.0,
            )
        except Exception:
            site_res = []
    for item in ((site_res or [{}])[0] or {}).get("items") or []:
        row = research._kw_row(item)
        keyword = str(row.get("keyword") or "").strip()
        key = keyword.lower()
        if not keyword or key in known:
            continue
        # Opportunity gaps: relevant site ideas you are not currently ranking for in the top 100.
        volume = row.get("volume")
        try:
            volume = int(volume) if volume is not None else None
        except (TypeError, ValueError):
            volume = None
        if not volume:
            continue
        gaps.append({"keyword": keyword, "volume": volume})
        known.add(key)
        if len(gaps) >= 8:
            break

    page_one = sum(1 for row in ranked if (row.get("position") or 99) <= 10)
    near = sum(1 for row in ranked if 11 <= (row.get("position") or 0) <= 20)
    visits = 0
    for row in ranked:
        vol = row.get("volume") or 0
        visits += int(round(vol * _ctr_share(row.get("position"))))

    return {
        "keywords": ranked[:12],
        "heatmap": ranked[:48],
        "gaps": gaps[:6],
        "visibility": {
            "searches": len(ranked),
            "pageOne": page_one,
            "nearMisses": near,
            "visits": visits,
            "market": location,
            "source": "dataforseo_ranked" if ranked else "",
        },
        "researchSource": "dataforseo" if ranked or gaps else "",
        "allRankedCount": len(ranked),
    }


def _preview_about(pages: list, brand: str, host: str, findings: list, category: str = "", city: str = "") -> str:
    """Plain-language gist from titles/headings/copy — no invented ranks."""
    home = next((p for p in pages if (urlparse(p.get("url") or "").path or "/") in {"", "/"}), pages[0] if pages else None)
    home_title = str((home or {}).get("title") or "").strip()
    missing_desc = sum(1 for f in findings if "description" in (f.get("label") or "").lower())
    bits = [f"We read {len(pages)} page{'s' if len(pages) != 1 else ''} the way Google does."]
    if category:
        bits.append(f"Your site reads as a {category}.")
    elif brand and brand.lower() not in {"home", "welcome"}:
        bits.append(f"The site presents itself as {brand}.")
    if city:
        if home_title and city.lower() not in home_title.lower():
            bits.append(f"Your homepage never says “{city}”, even though that market shows up on the site.")
        else:
            bits.append(f"We found {city} in your content.")
    else:
        bits.append("Google may be unsure which city or region to send you for.")
    if missing_desc:
        bits.append(
            f"{missing_desc} page{'s' if missing_desc != 1 else ''} "
            f"{'are' if missing_desc != 1 else 'is'} missing a description, so Google may invent the snippet."
        )
    elif findings:
        bits.append("A few listing details are thin — fixing them is usually a quick win.")
    else:
        bits.append("Basic titles and descriptions look in place; the next wins are usually local phrases and Search Console clicks.")
    return " ".join(bits)


def _preview_page_rows(pages: list) -> list[dict]:
    """Every crawled page for the free report table — including healthy ones."""
    rows = []
    for page in pages:
        color, label, detail = _preview_tone(page)
        path = urlparse(page.get("url") or "").path or "/"
        rows.append(
            {
                "color": color,
                "path": path,
                "label": label,
                "detail": detail,
                "title": str(page.get("title") or "").strip(),
                "words": int(page.get("words") or 0),
            }
        )
    order = {"red": 0, "yellow": 1, "green": 2}
    rows.sort(key=lambda item: (order.get(item["color"], 9), item.get("path") or ""))
    return rows


def _preview_opportunities(pages: list, brand: str, category: str, city: str, places: list, findings: list) -> list[dict]:
    """Conversion-ready problems even when titles/descriptions already pass."""
    out: list[dict] = []
    home = next((p for p in pages if (urlparse(p.get("url") or "").path or "/") in {"", "/"}), pages[0] if pages else None)
    home_title = str((home or {}).get("title") or "").strip()
    if city and home_title and city.lower() not in home_title.lower():
        out.append(
            {
                "color": "red",
                "path": "/",
                "label": f'Your homepage title never says “{city}”',
                "detail": f'Your title is the headline Google shows in search. Right now it says “{home_title or "Home"}”.',
                "title": home_title,
            }
        )
    if category and home_title and not any(token in home_title.lower() for token in category.lower().split()[:2] if len(token) > 3):
        out.append(
            {
                "color": "red",
                "path": "/",
                "label": f'Your homepage title never says "{category.split()[0]}"',
                "detail": f"People searching for a {category} need that phrase in the title Google shows.",
                "title": home_title,
            }
        )
    missing_desc = [f for f in findings if "description" in (f.get("label") or "").lower()]
    if missing_desc:
        out.append(
            {
                "color": "red",
                "path": "",
                "label": f"{len(missing_desc)} pages have no description in Google",
                "detail": "Google is filling in the blanks with random text from the page, so fewer people click.",
                "title": "",
            }
        )
    thin = [p for p in pages if int(p.get("words") or 0) and int(p.get("words") or 0) < 120]
    if thin:
        out.append(
            {
                "color": "yellow",
                "path": urlparse(thin[0].get("url") or "").path or "/",
                "label": f"{len(thin)} pages look thin for Google",
                "detail": "Short pages rarely win competitive local searches. Adding clear service copy usually helps.",
                "title": str(thin[0].get("title") or ""),
            }
        )
    if places:
        top = places[0]
        out.append(
            {
                "color": "yellow",
                "path": "",
                "label": f"{top.get('name') or 'A nearby rival'} already has stronger local proof",
                "detail": (
                    f"They show {top.get('rating') or '—'}★ from {top.get('reviews') or '0'} reviews. "
                    "Connect Google and we’ll show the searches they’re winning that you aren’t."
                ),
                "title": "",
            }
        )
    elif category:
        out.append(
            {
                "color": "yellow",
                "path": "",
                "label": f"Local {category} rivals are already collecting reviews",
                "detail": "Without Search Console we can’t see the exact clicks you lose — Solo unlocks that list.",
                "title": "",
            }
        )
    # Dedupe by label and keep strongest first.
    seen: set[str] = set()
    unique = []
    for row in out:
        key = str(row.get("label") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _preview_gaps_from_site(pages: list, category: str, city: str) -> list[dict]:
    """Search phrases people would type, derived from the site — volumes stay empty until research runs."""
    phrases: list[str] = []
    if category and city:
        phrases.extend(
            [
                f"{category} {city}",
                f"best {category} {city}",
                f"{category} near me",
                f"{category.split()[0]} cost {city}" if category.split() else f"{category} {city}",
            ]
        )
    elif category:
        phrases.extend([category, f"best {category}", f"{category} near me"])
    nav = []
    for page in pages[:6]:
        for item in page.get("nav") or []:
            label = " ".join(str(item or "").split())
            if 3 < len(label) < 48 and label.lower() not in {"home", "contact", "about", "blog"}:
                nav.append(label)
    for label in dict.fromkeys(nav):
        phrase = f"{label} {city}".strip() if city else label
        phrases.append(phrase.lower())
        if len(phrases) >= 10:
            break
    out = []
    seen: set[str] = set()
    for phrase in phrases:
        key = phrase.lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"keyword": phrase, "volume": None, "source": "site_crawl", "note": "volume after Solo"})
        if len(out) >= 6:
            break
    return out


def _seed_keywords(keywords: list, gaps: list, category: str) -> list[str]:
    """Real searches for this business. A schema label such as Web Site is never a seed."""
    seeds: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        text = " ".join(str(raw or "").split())
        key = text.lower()
        if not text or key in seen or not _usable_category(text):
            return
        seen.add(key)
        seeds.append(text)

    for row in sorted(gaps or [], key=lambda item: -(int(item.get("volume") or 0))):
        add(row.get("keyword") or "")
        if len(seeds) >= 1:
            break
    add(category)
    for row in sorted(keywords or [], key=lambda item: -(int(item.get("volume") or 0))):
        add(row.get("keyword") or "")
        if len(seeds) >= 2:
            break
    return seeds[:2]


def _rivals_from_rows(host: str, grouped: dict[str, list], visits: int) -> list[dict]:
    """Turn organic results for the site's own searches into the rival bars."""
    tallies: dict[str, dict] = {}
    for keyword, rows in grouped.items():
        for row in rows or []:
            rival_host = host_of(str(row.get("url") or ""))
            if not rival_host or rival_host == host or any(skip in rival_host for skip in _SKIP_RIVAL_HOSTS):
                continue
            try:
                rank = int(row.get("rank") or 99)
            except (TypeError, ValueError):
                rank = 99
            slot = tallies.setdefault(
                rival_host,
                {"name": rival_host, "label": rival_host, "best": 99, "terms": [], "you": False},
            )
            if keyword not in slot["terms"]:
                slot["terms"].append(keyword)
            if rank < slot["best"]:
                slot["best"] = rank
    rivals = sorted(tallies.values(), key=lambda item: (-len(item["terms"]), item["best"], item["name"]))[:4]
    for row in rivals:
        best = row["best"] if row["best"] < 99 else 10
        row["score"] = max(1, (11 - min(best, 10)) + 4 * max(0, len(row["terms"]) - 1))
        lead = row["terms"][0] if row["terms"] else ""
        row["signal"] = f"#{best} for {lead}" if len(row["terms"]) == 1 else f"#{best} · {len(row['terms'])} searches"
        row["address"] = ""
        row["visits"] = None
    you_score = max(1, int(visits) if visits else 1)
    rivals.append(
        {
            "name": host,
            "label": f"You · {host}",
            "score": you_score,
            "you": True,
            "visits": visits or None,
            "terms": [],
            "best": None,
            "signal": f"{visits} / mo est." if visits else "your site",
            "address": "",
        }
    )
    top = max((row["score"] for row in rivals), default=1)
    for row in rivals:
        row["pct"] = max(6, round(100 * row["score"] / top))
    return rivals


def _preview_search_rivals(
    host: str,
    keywords: list,
    gaps: list,
    category: str,
    country: str,
    language: str,
    visits: int,
) -> tuple[list[dict], str]:
    """Who ranks in this site's country for its real keywords and category."""
    seeds = _seed_keywords(keywords, gaps, category)
    if not seeds or not country:
        return [], ""
    try:
        from app import dataforseo
    except Exception:
        return [], ""
    if not dataforseo.configured():
        return [], ""
    grouped: dict[str, list] = {}
    for seed in seeds:
        grouped[seed] = dataforseo.competitor_serp(
            seed,
            location_name=country,
            language_code=language or "en",
            depth=10,
        )
    rivals = _rivals_from_rows(host, grouped, visits)
    if len(rivals) <= 1:
        return [], ""
    return rivals, seeds[0]


def _preview_competitors(host: str, places: list, visits: int) -> list[dict]:
    """Bars for nearby Places listings + you. Uses review signal when traffic is unknown."""
    rivals = []
    for place in places[:4]:
        name = str(place.get("name") or "").strip()
        if not name:
            continue
        try:
            rating = float(str(place.get("rating") or "0").replace("★", "").strip() or 0)
        except ValueError:
            rating = 0.0
        try:
            reviews = int(str(place.get("reviews") or "0").replace(",", "").split()[0] or 0)
        except ValueError:
            reviews = 0
        score = max(1, int(round(rating * max(reviews, 1))))
        site = str(place.get("website") or "").strip()
        label = host_of(site) if site else name
        rivals.append(
            {
                "name": label or name,
                "label": name,
                "score": score,
                "you": False,
                "visits": None,
                "rating": place.get("rating"),
                "reviews": place.get("reviews"),
                "address": place.get("address") or "",
            }
        )
    you_score = max(1, int(visits) if visits else 8)
    rivals.append({"name": host, "label": f"You · {host}", "score": you_score, "you": True, "visits": visits or None})
    top = max((r["score"] for r in rivals), default=1)
    for row in rivals:
        row["pct"] = max(6, round(100 * row["score"] / top))
        if row.get("you") and visits:
            row["signal"] = f"{visits} / mo est."
        elif row.get("you"):
            row["signal"] = "your site"
        elif row.get("rating") not in (None, "", "—"):
            row["signal"] = f"{row.get('rating')}★ · {row.get('reviews') or 0} reviews"
        else:
            row["signal"] = f"{row['score']} local signal"
    return rivals


_ONPAGE_TOPIC = {
    "no_title": "title",
    "title_too_long": "title",
    "title_too_short": "title",
    "duplicate_title": "title",
    "duplicate_title_tag": "title",
    "irrelevant_title": "title",
    "no_description": "description",
    "description_too_long": "description",
    "description_too_short": "description",
    "duplicate_description": "description",
    "irrelevant_description": "description",
    "no_h1_tag": "heading",
    "no_image_alt": "image",
    "no_image_title": "image",
    "has_render_blocking_resources": "css",
    "high_loading_time": "speed",
    "high_waiting_time": "ssr",
    "large_page_size": "speed",
    "size_greater_than_3mb": "speed",
    "low_content_rate": "content",
    "low_character_count": "content",
    "has_micromarkup_errors": "heading",
    "is_4xx_code": "grade",
    "is_5xx_code": "grade",
}


def _preview_onpage(home: str) -> dict:
    """DataForSEO On-Page instant audit of the homepage. Empty when the API is not connected."""
    try:
        from app import research
    except Exception:
        return {}
    if not research.configured():
        return {}
    try:
        result, _cost = research._call(
            "POST",
            "/on_page/instant_pages",
            [{"url": home, "enable_javascript": False, "enable_browser_rendering": False}],
            timeout=40.0,
        )
    except Exception as exc:
        log.warning("preview_onpage_failed", extra={"error": str(exc)[:180]})
        return {}
    item = next(iter(((result or [{}])[0] or {}).get("items") or []), None)
    if not isinstance(item, dict):
        return {}
    checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    issues: list[dict] = []
    for key, (name, severity, fix, _meta_flag) in research.PAGE_CHECKS.items():
        if checks.get(key) is not True:
            continue
        if severity == "Notice" and key not in {"title_too_long", "title_too_short", "description_too_long", "description_too_short"}:
            continue
        title = str(meta.get("title") or "")[:180]
        detail = fix
        if key.startswith("title") and title:
            detail = f"{fix} Live title: “{title}”."
        issues.append(
            {
                "topic": _ONPAGE_TOPIC.get(key, "grade"),
                "color": "red" if severity == "Critical" else "yellow",
                "path": urlparse(str(item.get("url") or home)).path or "/",
                "label": name,
                "detail": detail[:240],
                "title": title,
                "source": "dataforseo_onpage",
                "key": key,
            }
        )
    raw_score = item.get("onpage_score")
    try:
        score = round(float(raw_score), 1) if raw_score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "source": "dataforseo_on_page",
        "score": score,
        "url": str(item.get("url") or home)[:300],
        "issues": issues[:12],
    }


_GENERIC_CATEGORY = {
    "website", "webpage", "web site", "web page", "homepage", "home page", "organization",
    "local business", "localbusiness", "site", "blog", "home", "page", "menu", "welcome",
    "breadcrumblist", "imageobject", "searchaction", "entrypoint", "listitem", "wpheader",
    "wpfooter", "person", "postaladdress",
}
_SKIP_RIVAL_HOSTS = ("google.", "youtube.com", "facebook.com", "instagram.com", "wikipedia.org", "pinterest.com")


def _usable_category(text: str) -> str:
    """A business category from the site. Schema types like WebSite are not a category."""
    cleaned = " ".join(str(text or "").split())
    key = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
    if not cleaned or len(cleaned) < 3 or key in {re.sub(r"[^a-z0-9]+", "", item) for item in _GENERIC_CATEGORY}:
        return ""
    return cleaned[:80]


def _schema_category(pages: list) -> str:
    for page in pages[:4]:
        for kind in page.get("schemaTypes") or []:
            label = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(kind or "")).strip()
            usable = _usable_category(label)
            if usable:
                return usable
    return ""


def _preview_business(pages: list) -> dict:
    """Read the crawled pages into one business summary. Empty when the model is not available."""
    if not pages or not openai_configured():
        return {}
    data = understand(pages[:18], {}, {})
    if not isinstance(data, dict) or data.get("_error"):
        return {"error": str((data or {}).get("_error") or "")}
    places = [str(p).strip() for p in (data.get("places") or []) if str(p).strip()]
    offers = [str(o).strip() for o in (data.get("offers") or []) if str(o).strip()]
    return {
        "brand": str(data.get("brand") or "").strip()[:120],
        "category": str(data.get("category") or "").strip()[:80],
        "city": places[0][:80] if places else "",
        "places": places[:6],
        "offers": offers[:12],
        "audience": str(data.get("audience") or "").strip()[:160],
        "sentence": str(data.get("business") or "").strip()[:400],
        "voice": str(data.get("voice") or "").strip()[:80],
        "model": str(data.get("_model") or ""),
    }


def preview_scan(home: str, limit: int = 40) -> dict:
    """Free public deep scan: sitemap + internal links, heatmap grade, optional live ranked keywords."""
    deep = max(1, min(int(limit or 40), 50))
    crawled = crawl(home, limit=deep)
    pages = crawled.get("pages") or []
    failed = crawled.get("failed") or []
    findings = []
    dots = []
    counts = {"red": 0, "yellow": 0, "green": 0, "blue": 0}
    titles = []
    page_rows = _preview_page_rows(pages)
    for page in pages:
        color, label, detail = _preview_tone(page)
        counts[color] = counts.get(color, 0) + 1
        path = urlparse(page.get("url") or "").path or "/"
        dots.append({"color": color, "path": path})
        title = str(page.get("title") or "").strip()
        if title:
            titles.append(title)
        if color != "green":
            low = label.lower()
            topic = (
                "title" if "title" in low else
                "description" if "description" in low else
                "heading" if "heading" in low else
                "content"
            )
            findings.append({
                "color": color,
                "path": path,
                "label": label,
                "detail": detail,
                "title": title,
                "topic": topic,
                "source": "crawl",
            })
    broken = [row for row in failed if str(row.get("error") or "").startswith("HTTP 4") or str(row.get("error") or "").startswith("HTTP 5")]
    if broken:
        counts["red"] = counts.get("red", 0) + min(len(broken), 6)
        for row in broken[:6]:
            path = urlparse(row.get("url") or "").path or "/"
            dots.append({"color": "red", "path": path})
            findings.append({
                "color": "red",
                "path": path,
                "label": "Broken page",
                "detail": f"A linked URL returned {row.get('error') or 'an error'}.",
                "title": "",
            })
    order = {"red": 0, "yellow": 1, "green": 2}
    host = crawled.get("host") or host_of(home)
    platform = _preview_platform(pages)
    scan_home = crawled.get("home") or home
    country, language, market_source = _detect_market(scan_home, pages)
    # PageSpeed, ranked keywords, On-Page, and the business read are independent after the crawl.
    with ThreadPoolExecutor(max_workers=4) as pool:
        speed_job = pool.submit(_preview_pagespeed, scan_home)
        rank_job = pool.submit(_preview_visibility, host, pages, [], location=country, language=language)
        onpage_job = pool.submit(_preview_onpage, scan_home)
        business_job = pool.submit(_preview_business, pages)
        pagespeed = speed_job.result() or {}
        visibility_pack = rank_job.result() or {}
        onpage = onpage_job.result() or {}
        business = business_job.result() or {}
    schema_name = next((str(p.get("schemaName") or "").strip() for p in pages if p.get("schemaName")), "")
    site_name = next((str(p.get("siteName") or "").strip() for p in pages if p.get("siteName")), "")
    title_brand = titles[0].split("|")[0].split("–")[0].split("-")[0].strip() if titles else ""
    if title_brand and len(title_brand.split()) > 5:
        title_brand = ""
    brand = (
        str(business.get("brand") or "").strip()
        or site_name
        or schema_name
        or title_brand
        or host.split(".")[0].replace("-", " ")
    )
    city = str(business.get("city") or "").strip() or _detect_city(pages, country)
    category = (
        _usable_category(business.get("category") or "")
        or _schema_category(pages)
        or _usable_category(_preview_category(pages, brand))
    )
    visibility = visibility_pack.get("visibility") or {}
    if country:
        visibility = {**visibility, "market": country, "language": language, "marketSource": market_source}
    keywords = visibility_pack.get("keywords") or []
    # Gaps stay empty unless DataForSEO returned searches this site does not rank for.
    gaps = visibility_pack.get("gaps") or []
    visits = int(visibility.get("visits") or 0)
    competitors, rival_query = _preview_search_rivals(host, keywords, gaps, category, country, language, visits)
    rival_source = "search" if competitors else ""
    places = []
    if not competitors and category and city:
        places = _preview_places(host, brand, category=category, city=city)
        if places:
            competitors = _preview_competitors(host, places, visits)
            rival_query = str(places[0].get("query") or "")
            rival_source = "places"
    rendering = _preview_rendering(pages, pagespeed)
    markup = _preview_markup_quality(pages, str((pages[0] or {}).get("renderHints", {}).get("classSample") or "") if pages else "")
    # Fold PageSpeed + On-Page + SSR/CSR/image + markup into findings.
    for issue in [
        *(onpage.get("issues") or []),
        *(pagespeed.get("issues") or []),
        *(rendering.get("issues") or []),
        *(markup.get("issues") or []),
    ]:
        color = str(issue.get("color") or "yellow")
        if color in counts:
            counts[color] = counts.get(color, 0) + 1
        dots.append({"color": color, "path": str(issue.get("path") or "/"), "topic": issue.get("topic")})
        findings.append(
            {
                "color": color,
                "path": str(issue.get("path") or "/"),
                "label": issue.get("label") or "PageSpeed issue",
                "detail": issue.get("detail") or "",
                "title": issue.get("title") or "",
                "topic": issue.get("topic") or "speed",
                "source": issue.get("source") or "pagespeed",
            }
        )
    findings.sort(key=lambda item: order.get(item["color"], 9))
    health = _google_health(
        page_rows=page_rows,
        ranked=visibility_pack.get("heatmap") or keywords,
        onpage=onpage,
        pagespeed=pagespeed,
    )
    counts = health["counts"]
    dots = health["dots"]
    grade, gradeColor, gradeNote = health["grade"], health["gradeColor"], health["gradeNote"]

    # Topic checklist for FreeAnalysis major-issue showcase (HTML layout + tech topics).
    topic_keys = (
        "title",
        "description",
        "heading",
        "typography",
        "bem",
        "css",
        "javascript",
        "image",
        "lcp",
        "cls",
        "ssr",
        "csr",
        "places",
        "grade",
    )
    topic_status = []
    for key in topic_keys:
        if key == "places":
            if rival_source == "search" and len(competitors) > 1:
                topic_status.append(
                    {
                        "topic": "places",
                        "status": "ok",
                        "color": "green",
                        "label": f"{len(competitors) - 1} sites ranking for the same searches",
                        "detail": rival_query or category,
                    }
                )
            elif places:
                topic_status.append(
                    {
                        "topic": "places",
                        "status": "ok",
                        "color": "green",
                        "label": f"{len(places)} Google Places rivals nearby",
                        "detail": (str(places[0].get("query") or "") if places else "") or category,
                    }
                )
            else:
                topic_status.append(
                    {
                        "topic": "places",
                        "status": "warning",
                        "color": "yellow",
                        "label": "No Google Places rivals found yet",
                        "detail": "Category or city was unclear from the crawl.",
                    }
                )
            continue
        if key == "grade":
            tone = "green"
            if grade in ("D", "D+", "F", "C-", "C"):
                tone = "yellow"
            if grade in ("D-", "F") or (counts.get("red") or 0) >= 4:
                tone = "red"
            topic_status.append(
                {
                    "topic": "grade",
                    "status": "error" if tone == "red" else ("warning" if tone == "yellow" else "ok"),
                    "color": tone,
                    "label": f"Google health {grade}",
                    "detail": " · ".join(part for part in (gradeNote, health.get("gradeDetail")) if part),
                }
            )
            continue
        hits = [f for f in findings if str(f.get("topic") or "") == key]
        if not hits:
            topic_status.append({"topic": key, "status": "ok", "color": "green", "label": "Looking fine", "detail": ""})
            continue
        worst = hits[0]
        topic_status.append(
            {
                "topic": key,
                "status": "error" if worst.get("color") == "red" else "warning",
                "color": worst.get("color") or "yellow",
                "label": worst.get("label") or key,
                "detail": worst.get("detail") or "",
            }
        )
    about = _preview_about(pages, brand, host, findings, category=category, city=city)
    if business.get("sentence"):
        extra = []
        if business.get("audience"):
            extra.append(f"They sell to {business['audience']}.")
        if business.get("offers"):
            extra.append("Offers on the site: " + ", ".join(business["offers"][:6]) + ".")
        about = " ".join([business["sentence"], *extra, about])
    opportunities = _preview_opportunities(pages, brand, category, city, places, findings)
    name_bit = brand.split()[0] if brand else "there"
    if visibility.get("searches"):
        headline = (
            f"{name_bit}, Google finds you for {visibility['searches']} searches. "
            f"You're on page one for {visibility.get('pageOne') or 0} of them."
        )
    elif places and category:
        top = places[0].get("name") or "local rivals"
        headline = (
            f"{name_bit}, your site reads as a {category}"
            + (f" in {city}" if city else "")
            + f". Nearby, {top} is already collecting the local trust signals."
        )
    else:
        urgent = counts.get("red") or 0
        work = counts.get("yellow") or 0
        if urgent:
            headline = f"{name_bit}, we checked {len(pages)} pages on {host}. {urgent} need urgent fixes"
            headline += f", and {work} need work." if work else "."
        elif work:
            headline = f"{name_bit}, we checked {len(pages)} pages on {host}. {work} pages need work."
        else:
            headline = (
                f"{name_bit}, we checked {len(pages)} pages on {host}. "
                f"Listings look tidy — the next wins are the searches and local rivals Solo unlocks."
            )
    problems = []
    for row in [*opportunities, *findings]:
        key = str(row.get("label") or "")
        if any(key == str(p.get("label") or "") for p in problems):
            continue
        problems.append(row)
        if len(problems) >= 3:
            break
    if not problems:
        problems = [
            {
                "color": "yellow",
                "path": "",
                "label": "Public crawl can’t see the searches you’re losing",
                "detail": "Connect Google Search Console and we’ll list the exact queries and the fixes to approve.",
                "title": "",
            }
        ]
    more_issues = max(0, len(findings) + len(opportunities) - len(problems))
    places_query = rival_query or (f"{category} in {city}" if category and city else "")
    return {
        "home": crawled.get("home") or home,
        "host": host,
        "pagesRead": len(pages),
        "pagesDiscovered": len(pages) + len(failed),
        "counts": counts,
        "dots": dots[:48],
        "findings": findings[:24],
        "pageRows": page_rows[:24],
        "problems": problems,
        "moreIssues": more_issues,
        "error": crawled.get("error") or "",
        "platform": platform,
        "grade": grade,
        "gradeColor": gradeColor,
        "gradeNote": gradeNote,
        "gradeDetail": health.get("gradeDetail") or "",
        "healthKind": health.get("kind") or "pages",
        "healthScore": health.get("score"),
        "brand": brand,
        "category": category,
        "city": city,
        "headline": headline,
        "aboutGoogle": about,
        "places": places,
        "placesQuery": places_query,
        "rivalSource": rival_source,
        "rivalQuery": rival_query,
        "placesSource": "google_places" if places else "",
        "keywords": keywords,
        "gaps": gaps,
        "competitors": competitors,
        "visibility": visibility,
        "researchSource": visibility_pack.get("researchSource") or "",
        "pagespeed": pagespeed or None,
        "pagespeedSource": pagespeed.get("source") if pagespeed else "",
        "onpage": onpage or None,
        "onpageSource": onpage.get("source") if onpage else "",
        "onpageScore": onpage.get("score") if onpage else None,
        "business": {k: v for k, v in business.items() if k != "error" and v} or None,
        "offers": business.get("offers") or [],
        "language": language,
        "country": country,
        "rendering": rendering or None,
        "markup": markup or None,
        "topicStatus": topic_status,
        "deepScan": True,
        "scanLimit": deep,
        "scanVersion": "free-deep-v8",
    }
