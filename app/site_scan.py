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
        self.canonical = ""
        self.noindex = False
        self.skip = 0
        self.chrome = 0
        self.current = ""
        self.buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if tag in CHROME_TAGS:
            self.chrome += 1
        if tag == "a" and attr.get("href"):
            self.links.append(attr["href"])
        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = attr.get("content", "")
            if name == "robots" and "noindex" in attr.get("content", "").lower():
                self.noindex = True
        if tag == "link" and "canonical" in attr.get("rel", "").lower():
            self.canonical = attr.get("href", "")
        if tag in {"title", "h1", "h2", "h3"} and not self.skip:
            self.current = tag
            self.buffer = []

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
            return
        if tag in CHROME_TAGS and self.chrome:
            self.chrome -= 1
        if tag == self.current:
            value = " ".join(" ".join(self.buffer).split())
            if value:
                if tag == "title":
                    self.title.append(value)
                elif tag == "h1":
                    self.h1.append(value)
                elif len(self.headings) < 14:
                    self.headings.append(value)
            self.current = ""
            self.buffer = []

    def handle_data(self, data):
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


def read_page(client: httpx.Client, url: str) -> dict:
    response = _get(client, url)
    if response is None:
        return {"url": url, "error": "could not load"}
    kind = response.headers.get("content-type", "")
    if "html" not in kind:
        return {"url": str(response.url), "error": "not html"}
    reader = PageReader(str(response.url))
    try:
        reader.feed(response.text[:600_000])
    except Exception:  # noqa: BLE001
        pass
    text = " ".join(reader.text)
    return {
        "url": normalize(str(response.url)),
        "title": " ".join(reader.title)[:200],
        "description": reader.description.strip()[:400],
        "h1": " | ".join(reader.h1)[:240],
        "headings": reader.headings[:14],
        "text": text[:4000],
        "words": len(text.split()),
        "canonical": urljoin(str(response.url), reader.canonical) if reader.canonical else "",
        "noindex": reader.noindex,
        "links": reader.links[:400],
        "error": "",
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
    seen: set[str] = set()
    with httpx.Client(headers={"User-Agent": AGENT}) as client, ThreadPoolExecutor(max_workers=6) as pool:
        rounds = 0
        while queue and len(pages) < limit and rounds < 4:
            rounds += 1
            batch = []
            for url in queue:
                if url not in seen:
                    seen.add(url)
                    batch.append(url)
                if len(batch) >= limit - len(pages):
                    break
            queue = [url for url in queue if url not in seen]
            results = list(pool.map(lambda url: read_page(client, url), batch))
            fresh_links: list[str] = []
            for page in results:
                if page.get("error"):
                    continue
                if page["url"] in {p["url"] for p in pages}:
                    continue
                for href in page.pop("links", []):
                    absolute = normalize(urljoin(page["url"], href.split("#", 1)[0]))
                    if absolute in seen or not _allowed(absolute, home_host):
                        continue
                    if robots is not None and not robots.can_fetch(AGENT, absolute):
                        continue
                    fresh_links.append(absolute)
                pages.append(page)
            queue = sorted(dict.fromkeys([*queue, *fresh_links]), key=_priority)
    for page in pages:
        page.pop("links", None)
    return {"home": home, "host": home_host, "pages": pages, "error": "" if pages else "No readable pages were found."}


UNDERSTAND = (
    "You are an SEO strategist reading a whole website before writing any Google listing. "
    "Use only what the pages and the owner's brief say. Do not invent awards, prices, years, reviews, or locations. "
    "Return JSON only, no prose, with this shape:\n"
    '{"business":"one sentence: who they are and what they sell",'
    '"offers":["main products or services, as the site names them"],'
    '"audience":"who buys",'
    '"places":["cities or regions the site serves, only if stated"],'
    '"proof":["concrete facts on the site a listing may use, e.g. same-day service, free quote, 24/7"],'
    '"voice":"how the brand talks, 3-6 words",'
    '"brand":"the brand name as written on the site",'
    '"pages":[{"url":"...","role":"home|service|product|category|location|about|contact|blog|legal|other",'
    '"target":"the one search this page should win, 2-6 words, in the customer\'s words",'
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
    for page in pages[:45]:
        digest.append(
            f"URL: {page['url']}\nTITLE: {page.get('title') or '-'}\nH1: {page.get('h1') or '-'}\n"
            f"HEADINGS: {' | '.join(page.get('headings') or [])[:300] or '-'}\n"
            f"COPY: {(page.get('text') or '')[:420]}"
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
        text, _model, _role = chat_text(system=UNDERSTAND, user=user[:30000], kind="seo", writing_type="Site understanding", tokens=1800, max_tokens=4000)
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
) -> dict:
    """Competitor listings per target search. Only real sources; an empty dict means none were available."""
    from app import dataforseo
    from app.research import location_for

    location_name = location_for(market)[0]
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
            if dataforseo.configured():
                for row in dataforseo.competitor_serp(target, location_name=location_name):
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


def scan_site(home: str, *, brief: dict, profile: dict, places_rows: list | None = None, limit: int = 40) -> dict:
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
    )
    return {
        "home": crawled.get("home"),
        "host": crawled.get("host"),
        "scannedAt": datetime.utcnow().isoformat(timespec="seconds"),
        "pages": pages,
        "business": {k: v for k, v in understanding.items() if k != "pages"},
        "pageMap": page_map,
        "competitors": competitors,
        "brief": brief,
        "error": crawled.get("error") or "",
    }
