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

import httpx

from app.openai_client import chat_text, openai_configured
from app.page_scrape import is_video_src, summarize_json_ld

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


def read_page(client: httpx.Client, url: str) -> dict:
    fetched = datetime.utcnow().isoformat(timespec="seconds")
    try:
        response = client.get(url, timeout=10.0, follow_redirects=True)
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
    text = " ".join(reader.text)
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
        "canonical": canonical,
        "canonicalElsewhere": bool(canonical) and page_key(canonical) != page_key(final),
        "noindex": reader.noindex,
        "contentHash": hashlib.sha256(
            f"{title}\n{description}\n{reader.og_title}\n{schema.get('schemaName')}\n{text}".encode()
        ).hexdigest()[:32],
        "links": reader.links[:400],
        "error": "",
        **schema,
        "faqs": faqs,
        "reviewRating": schema.get("reviewRating") or "",
        "reviewCount": schema.get("reviewCount") or "",
    }


def _sitemap_urls(client: httpx.Client, url: str, depth: int = 0, cap: int = 400) -> list[str]:
    if depth > 2:
        return []
    response = _get(client, url, timeout=10.0)
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
        for child in preferred[:12]:
            out.extend(_sitemap_urls(client, child, depth + 1, cap))
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


def discover(home: str, limit: int) -> tuple[list[str], RobotFileParser | None]:
    home = normalize(home)
    home_host = host_of(home)
    robots = RobotFileParser()
    sitemaps: list[str] = []
    found: list[str] = []
    with httpx.Client(headers={"User-Agent": AGENT}) as client:
        robots_txt = _get(client, urljoin(home, "/robots.txt"), timeout=6.0)
        if robots_txt is not None:
            lines = robots_txt.text.splitlines()
            robots.parse(lines)
            sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("sitemap:")]
        else:
            robots = None
        for candidate in [*sitemaps, urljoin(home, "/sitemap.xml"), urljoin(home, "/sitemap_index.xml"), urljoin(home, "/wp-sitemap.xml")]:
            urls = _sitemap_urls(client, candidate)
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


def crawl(home: str, limit: int = 40) -> dict:
    """Read up to `limit` pages. Sitemap first, then internal links from the pages already read."""
    home = normalize(home if home.startswith(("http://", "https://")) else f"https://{home}")
    if not public_host(home):
        return {"home": home, "pages": [], "error": "This address is not a public website."}
    home_host = host_of(home)
    queue, robots = discover(home, limit)
    queue = sorted(queue, key=_priority)
    pages: list[dict] = []
    failed: list[dict] = []
    seen: set[str] = set()
    keys: set[str] = set()
    with httpx.Client(headers={"User-Agent": AGENT}) as client, ThreadPoolExecutor(max_workers=6) as pool:
        rounds = 0
        while queue and len(pages) < limit and rounds < 4:
            rounds += 1
            batch = []
            for url in queue:
                if url not in seen and page_key(url) not in keys:
                    seen.add(url)
                    batch.append(url)
                if len(batch) >= limit - len(pages):
                    break
            queue = [url for url in queue if url not in seen]
            results = list(pool.map(lambda url: read_page(client, url), batch))
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
READER_VERSION = 2

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
        copy_room = 1600 if index == 0 else 700
        digest.append(
            f"URL: {page['url']}\nTITLE: {page.get('title') or '-'}\n"
            f"META DESCRIPTION: {page.get('description') or '-'}\n"
            f"OPEN GRAPH: {page.get('ogTitle') or '-'} — {page.get('ogDescription') or '-'}\n"
            f"JSON-LD: {schema}; name {page.get('schemaName') or '-'}\n"
            f"H1: {page.get('h1') or '-'}\n"
            f"NAVIGATION: {', '.join(page.get('nav') or [])[:300] or '-'}\n"
            f"HEADINGS: {' | '.join(page.get('headings') or [])[:500] or '-'}\n"
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
        text, _model, _role = chat_text(system=UNDERSTAND, user=user[:60000], kind="seo", writing_type="Site understanding", tokens=2400, max_tokens=9000)
    except RuntimeError:
        return {}
    return _json_block(text)


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
) -> dict:
    crawled = crawl(home, limit=limit)
    pages = crawled.get("pages") or []
    understanding = understand(pages, brief, profile)
    page_map = {normalize(item.get("url") or ""): item for item in understanding.get("pages") or [] if isinstance(item, dict)}
    targets = [
        str(page_map.get(page["url"], {}).get("target") or "").strip()
        for page in pages
        if page_map.get(page["url"], {}).get("role") not in {"legal", "contact", "about", "blog"}
    ]
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
        # Bump when the site reader changes so stored scans are re-read once (operator._scan_fresh).
        "readerVersion": READER_VERSION,
        "pageMap": page_map,
        "competitors": competitors,
        "brief": brief,
        "failed": crawled.get("failed") or [],
        "error": crawled.get("error") or "",
    }


def _preview_tone(page: dict) -> tuple[str, str, str]:
    """Colour from what the page actually has. No invented scores."""
    title = str(page.get("title") or "").strip()
    description = str(page.get("description") or "").strip()
    if page.get("noindex"):
        return "red", "Hidden from Google", "This page asks Google not to index it."
    if not title:
        return "red", "Missing title", "Google has no title to show for this page."
    if not description:
        return "yellow", "Missing description", "The search snippet will be chosen by Google."
    if len(title) < 12 or len(title) > 65:
        return "yellow", "Title length", "The title is hard to read in a search result."
    if not str(page.get("h1") or "").strip():
        return "yellow", "Missing heading", "The page has no H1."
    return "green", "Listing is in place", "A title and description are on the page."


def preview_scan(home: str, limit: int = 8) -> dict:
    """Free public look: a few live pages, colour-coded. No AI and no paid research."""
    crawled = crawl(home, limit=max(1, min(int(limit or 8), 12)))
    pages = crawled.get("pages") or []
    findings = []
    dots = []
    counts = {"red": 0, "yellow": 0, "green": 0, "blue": 0}
    for page in pages:
        color, label, detail = _preview_tone(page)
        counts[color] = counts.get(color, 0) + 1
        path = urlparse(page.get("url") or "").path or "/"
        dots.append({"color": color, "path": path})
        if color != "green":
            findings.append({
                "color": color,
                "path": path,
                "label": label,
                "detail": detail,
                "title": str(page.get("title") or "").strip(),
            })
    order = {"red": 0, "yellow": 1, "green": 2}
    findings.sort(key=lambda item: order.get(item["color"], 9))
    return {
        "home": crawled.get("home") or home,
        "host": crawled.get("host") or host_of(home),
        "pagesRead": len(pages),
        "counts": counts,
        "dots": dots[:40],
        "findings": findings[:6],
        "error": crawled.get("error") or "",
    }
