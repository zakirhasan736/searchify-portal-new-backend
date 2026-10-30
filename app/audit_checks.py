"""Extra site-audit checks, each returned as an audit *issue*:

    {key, name, severity: Critical|Warning|Notice, area, fix, steps[], helpUrl,
     count, pages: [{url, status, title, detail}], source}

Sources:
- Searchify SEO crawl (DataForSEO On-Page): status codes, redirect chains, indexability,
  canonicals, spelling, broken images/CSS/scripts, heading duplicates.
- Site scan (our own reader, raw HTML): heading order by section, JSON-LD schema
  validation (FAQ, product price, reviews, breadcrumbs, business, article, video, image),
  accordions without FAQ schema, image dimensions/file names.
- Site checks: robots.txt, XML sitemap, http→https and www redirects.
- Google Search Console URL Inspection: coverage, fetch errors, canonical chosen by
  Google, rich-result (schema) errors as Google sees them.

Every check is deterministic and explains the exact page and the exact fix. Nothing here
edits a site.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# --- Google documentation (verified, stable URLs) ---------------------------------------

DOC = {
    "status": "https://developers.google.com/search/docs/crawling-indexing/http-network-errors",
    "redirects": "https://developers.google.com/search/docs/crawling-indexing/301-redirects",
    "canonical": "https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls",
    "robots": "https://developers.google.com/search/docs/crawling-indexing/robots/intro",
    "robots_create": "https://developers.google.com/search/docs/crawling-indexing/robots/create-robots-txt",
    "noindex": "https://developers.google.com/search/docs/crawling-indexing/block-indexing",
    "sitemap": "https://developers.google.com/search/docs/crawling-indexing/sitemaps/overview",
    "title": "https://developers.google.com/search/docs/appearance/title-link",
    "snippet": "https://developers.google.com/search/docs/appearance/snippet",
    "images": "https://developers.google.com/search/docs/appearance/google-images",
    "faq": "https://developers.google.com/search/docs/appearance/structured-data/faqpage",
    "product": "https://developers.google.com/search/docs/appearance/structured-data/product",
    "review": "https://developers.google.com/search/docs/appearance/structured-data/review-snippet",
    "breadcrumb": "https://developers.google.com/search/docs/appearance/structured-data/breadcrumb",
    "article": "https://developers.google.com/search/docs/appearance/structured-data/article",
    "business": "https://developers.google.com/search/docs/appearance/structured-data/local-business",
    "video": "https://developers.google.com/search/docs/appearance/structured-data/video",
    "rich_test": "https://search.google.com/test/rich-results",
    "schema_validator": "https://validator.schema.org/",
    "page_indexing": "https://support.google.com/webmasters/answer/7440203",
}

SEVERITY_ORDER = {"Critical": 0, "Warning": 1, "Notice": 2}


def issue(
    key: str,
    name: str,
    severity: str,
    area: str,
    fix: str,
    steps: list[str],
    pages: list[dict],
    *,
    help_url: str = "",
    source: str = "crawl",
    count: int | None = None,
    meta: bool = False,
) -> dict:
    return {
        "key": key,
        "name": name,
        "severity": severity,
        "area": area,
        "fix": fix,
        "steps": steps,
        "helpUrl": help_url,
        "meta": meta,
        "count": count if count is not None else len(pages),
        "pages": pages[:50],
        "source": source,
    }


def page_row(url: str, *, status=None, title: str = "", detail: str = "") -> dict:
    row = {"url": url or "", "status": status, "title": (title or "")[:200], "description": ""}
    if detail:
        row["detail"] = detail[:600]
    return row


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


# ========================================================================================
# 1. Searchify SEO crawl (DataForSEO On-Page)
# ========================================================================================

STATUS_TEXT = {
    400: "Bad request", 401: "Login required", 403: "Forbidden", 404: "Not found", 405: "Method not allowed",
    408: "Request timeout", 410: "Gone", 429: "Too many requests", 451: "Unavailable for legal reasons",
    500: "Server error", 501: "Not implemented", 502: "Bad gateway", 503: "Service unavailable",
    504: "Gateway timeout", 520: "Unknown CDN error", 521: "Web server is down", 522: "Connection timed out",
    523: "Origin unreachable", 524: "A timeout occurred", 525: "SSL handshake failed", 526: "Invalid SSL certificate",
}

STATUS_FIX = {
    404: (
        "Restore the page, or 301-redirect it to the closest live page. Then fix the links that point to it.",
        [
            "Open the page list below and the pages that link to each one.",
            "If the content moved, add a 301 redirect from the old URL to the new one.",
            "If it is gone for good and has no replacement, leave it as 404 (or 410) but remove every internal link to it.",
            "Update menus, buttons and the sitemap so they only list live pages.",
            "Crawl again to confirm.",
        ],
    ),
    410: (
        "The page is marked Gone. Remove every internal link and sitemap entry that still points to it.",
        ["Remove the URL from menus, links and the sitemap.", "If there is a close replacement, 301-redirect to it instead.", "Crawl again."],
    ),
    401: (
        "The page asks for a login, so Google can't read it. Remove login protection from pages that should rank.",
        ["Check whether the page is meant to be public.", "If yes, remove the password / membership rule for that page.", "If not, remove it from the sitemap and internal links."],
    ),
    403: (
        "The server refuses the page. Usually a firewall, security plugin or CDN rule blocks crawlers.",
        [
            "Open the page in a private browser window. If it loads, a bot/firewall rule is blocking crawlers.",
            "In your security plugin or CDN (Cloudflare, Wordfence, Sucuri), allow Googlebot and Searchify's crawler.",
            "Check file permissions with your host if the page is blocked for everyone.",
            "Crawl again.",
        ],
    ),
    429: (
        "The server rate-limits crawlers. Google slows down or stops crawling when it sees 429.",
        ["Ask the host to raise the rate limit for verified Googlebot.", "Check the CDN/firewall bot rules.", "Crawl again later."],
    ),
    500: (
        "The page crashes on the server. Google drops pages that keep failing.",
        [
            "Open the page. If it errors for you too, check the PHP/server error log with your host or developer.",
            "In WordPress, a plugin or theme update is the usual cause: disable recently updated plugins one at a time.",
            "Fix the error, then crawl again.",
        ],
    ),
    502: (
        "Bad gateway: the CDN or proxy could not get an answer from your web server.",
        [
            "Check that the web server / PHP service is running (ask your host).",
            "If you use Cloudflare or another CDN, check its status and the origin server address.",
            "Repeated 502s often mean the server runs out of memory: ask the host to check resources.",
            "Crawl again after it is stable.",
        ],
    ),
    503: (
        "Service unavailable: the server is down, overloaded, or in maintenance mode.",
        [
            "Check whether a maintenance-mode plugin is still switched on.",
            "Ask the host whether the server hit its resource limits.",
            "Short planned downtime should return 503 with a Retry-After header; long or repeated 503s make Google drop the page.",
            "Crawl again once the site is up.",
        ],
    ),
    504: (
        "Gateway timeout: the server took too long to answer.",
        ["Find slow plugins or database queries with your developer.", "Enable page caching.", "Ask the host to check server load.", "Crawl again."],
    ),
}


def status_code_issues(items: list[dict], broken_links: list[dict] | None = None) -> list[dict]:
    """Group crawled pages by exact HTTP status (404, 410, 403, 500, 502, 503 …) with linking pages."""
    inbound: dict[str, list[str]] = {}
    for link in broken_links or []:
        to = str(link.get("to") or "")
        if to:
            inbound.setdefault(to.rstrip("/"), []).append(str(link.get("from") or ""))
    groups: dict[int, list[dict]] = {}
    for item in items:
        code = item.get("status_code")
        if not isinstance(code, int) or code < 400:
            continue
        url = item.get("url") or ""
        froms = [f for f in inbound.get(url.rstrip("/"), []) if f][:3]
        detail = f"Returns HTTP {code} {STATUS_TEXT.get(code, '')}.".replace(" .", ".")
        if froms:
            detail += " Linked from: " + ", ".join(froms) + ("…" if len(inbound.get(url.rstrip('/'), [])) > 3 else "")
        groups.setdefault(code, []).append(page_row(url, status=code, title=str((item.get("meta") or {}).get("title") or ""), detail=detail))
    out = []
    for code in sorted(groups):
        text = STATUS_TEXT.get(code, "Client error" if code < 500 else "Server error")
        fix, steps = STATUS_FIX.get(code) or STATUS_FIX[404 if code < 500 else 500]
        severity = "Critical" if code >= 500 or code in (404, 410, 401, 403) else "Warning"
        out.append(issue(
            f"status_{code}",
            f"Pages return {code} {text}",
            severity,
            "Status codes",
            fix,
            steps,
            groups[code],
            help_url=DOC["status"],
        ))
    temp = [
        page_row(i.get("url") or "", status=i.get("status_code"), detail=f"Temporary redirect ({i.get('status_code')}).")
        for i in items
        if i.get("status_code") in (302, 303, 307)
    ]
    if temp:
        out.append(issue(
            "temporary_redirects",
            "Pages use temporary redirects (302/307)",
            "Notice",
            "Redirects",
            "If the move is permanent, change the redirect to 301 (or 308) so Google moves the ranking to the new URL.",
            ["Open your redirect plugin or server rules.", "Change permanent moves from 302/307 to 301.", "Keep 302 only for truly temporary moves."],
            temp,
            help_url=DOC["redirects"],
        ))
    return out


def _hop_url(hop: dict, which: str) -> str:
    return str(hop.get(f"link_{which}") or hop.get(f"page_{which}") or "")


def redirect_chain_issues(chains: list[dict]) -> list[dict]:
    loops, long_chains, downgrade, to_broken = [], [], [], []
    for row in chains or []:
        hops = [h for h in (row.get("chain") or []) if isinstance(h, dict)]
        if not hops:
            continue
        path = [_hop_url(hops[0], "from")] + [_hop_url(h, "to") for h in hops]
        text = " → ".join(p for p in path if p)
        start = path[0]
        if row.get("is_redirect_loop"):
            loops.append(page_row(start, detail=f"Redirect loop: {text}. Browsers and Google give up on this URL."))
            continue
        if len(hops) >= 2:
            long_chains.append(page_row(start, detail=f"{len(hops)} redirects: {text}. Point links and the first redirect straight at the last URL."))
        if any(str(h.get("page_from_scheme")) == "https" and str(h.get("page_to_scheme")) == "http" for h in hops):
            downgrade.append(page_row(start, detail=f"Redirect goes from https to http: {text}."))
        if hops[-1].get("is_broken"):
            to_broken.append(page_row(start, detail=f"Redirect ends on a broken page: {text}."))
    out = []
    if loops:
        out.append(issue("redirect_loop", "Redirect loops", "Critical", "Redirects",
                         "Break the loop so the URL ends on one live page.",
                         ["Open each URL below in a browser and follow where it goes.", "In the redirect plugin or server rules, remove the rule that sends the page back to an earlier URL.",
                          "Each old URL should 301 to one final, live page.", "Crawl again."], loops, help_url=DOC["redirects"]))
    if to_broken:
        out.append(issue("redirect_to_broken", "Redirects lead to broken pages", "Critical", "Redirects",
                         "Point each redirect at a live page.",
                         ["Find the redirect rule for each URL below.", "Change the target to the closest live page.", "Crawl again."], to_broken, help_url=DOC["redirects"]))
    if long_chains:
        out.append(issue("redirect_chain_hops", "Redirect chains (more than one hop)", "Warning", "Redirects",
                         "Make every old URL redirect once, straight to the final URL, and update internal links to the final URL.",
                         ["For each chain below, note the last URL.", "Edit the first redirect so it goes straight to that last URL.",
                          "Update menus and in-page links to use the final URL.", "Crawl again."], long_chains, help_url=DOC["redirects"]))
    if downgrade:
        out.append(issue("redirect_https_to_http", "Redirects go from https to http", "Warning", "Redirects",
                         "Redirect to the https version of the page.",
                         ["Change the redirect target to the https:// URL.", "Turn on 'Always use HTTPS' at your host or CDN."], downgrade, help_url=DOC["redirects"]))
    return out


NON_INDEXABLE = {
    "robots_txt": ("Pages blocked by robots.txt", "Warning",
                   "If these pages should rank, remove the Disallow rule that matches them in robots.txt.",
                   ["Open yoursite/robots.txt.", "Find the Disallow line that matches the URLs below.", "Remove it or narrow it, keeping only private areas blocked.", "Crawl again."], "robots"),
    "meta_tag": ("Pages set to noindex (meta robots tag)", "Warning",
                 "If these pages should appear in Google, remove the noindex setting.",
                 ["In WordPress open the page → SEO plugin box → Advanced.", "Set 'Allow search engines to show this page' to Yes (Yoast) or remove 'No Index' (Rank Math).",
                  "Leave noindex only on thank-you, cart, login and similar pages.", "Crawl again."], "noindex"),
    "http_header": ("Pages set to noindex by an X-Robots-Tag header", "Warning",
                    "Remove the noindex X-Robots-Tag header for pages that should rank.",
                    ["Ask your developer or host which server/CDN rule adds 'X-Robots-Tag: noindex'.", "Remove it for public pages.", "Crawl again."], "noindex"),
    "attribute": ("Pages excluded by a canonical or link attribute", "Notice",
                  "Check that each page's canonical points to itself unless it is a true duplicate.",
                  ["Open the page source and find rel=canonical.", "If it points elsewhere by mistake, change it to the page's own URL."], "canonical"),
    "too_many_redirects": ("Pages with too many redirects", "Critical",
                           "Shorten the chain so the URL redirects once to a live page.",
                           ["Follow the URL in a browser.", "Remove extra redirect rules.", "Crawl again."], "redirects"),
}


def non_indexable_issues(items: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in items or []:
        reason = str(row.get("reason") or "other")
        groups.setdefault(reason, []).append(page_row(str(row.get("url") or ""), detail=f"Not indexable: {reason.replace('_', ' ')}."))
    out = []
    for reason, pages in groups.items():
        name, severity, fix, steps, doc = NON_INDEXABLE.get(
            reason, (f"Pages not indexable ({reason})", "Notice", "Check why Google can't index these pages.", ["Open each page and check robots, canonical and status."], "noindex")
        )
        out.append(issue(f"non_indexable_{reason}", name, severity, "Indexing", fix, steps, pages, help_url=DOC[doc]))
    return out


def canonical_issues(html_items: list[dict], site_host: str) -> list[dict]:
    missing, other_host, http_canon, elsewhere = [], [], [], []
    for item in html_items:
        url = item.get("url") or ""
        if not url or (item.get("status_code") or 200) >= 300:
            continue
        canon = str((item.get("meta") or {}).get("canonical") or "").strip()
        if not canon:
            missing.append(page_row(url, detail="No rel=canonical tag."))
            continue
        absolute = urljoin(url, canon)
        if _host(absolute) and site_host and _host(absolute) != site_host:
            other_host.append(page_row(url, detail=f"Canonical points to another domain: {absolute}"))
        elif url.startswith("https://") and absolute.startswith("http://"):
            http_canon.append(page_row(url, detail=f"Canonical uses http: {absolute}"))
        elif absolute.rstrip("/") != url.rstrip("/"):
            elsewhere.append(page_row(url, detail=f"Canonical points to {absolute}. Google may index that URL instead of this one."))
    out = []
    if other_host:
        out.append(issue("canonical_other_domain", "Canonical points to another domain", "Critical", "Canonicals",
                         "Point the canonical to this site's own URL unless the content truly belongs on the other domain.",
                         ["Open the SEO plugin's Advanced/canonical field for each page.", "Clear the wrong value so the page uses its own URL.",
                          "Check staging/old-domain settings in WordPress → Settings → General.", "Crawl again."], other_host, help_url=DOC["canonical"]))
    if http_canon:
        out.append(issue("canonical_http", "Canonical uses http on an https site", "Warning", "Canonicals",
                         "Use the https URL in the canonical.",
                         ["Set WordPress Address and Site Address to https in Settings → General.", "Re-save the SEO plugin settings.", "Crawl again."], http_canon, help_url=DOC["canonical"]))
    if missing:
        out.append(issue("canonical_missing", "Pages have no canonical tag", "Notice", "Canonicals",
                         "Add a self-referencing canonical so Google knows the main URL of each page.",
                         ["Most SEO plugins (Yoast, Rank Math) add it automatically: check the plugin is active on these pages.",
                          "For custom pages, add <link rel=\"canonical\" href=\"https://…the page URL…\">.", "Crawl again."], missing, help_url=DOC["canonical"]))
    if elsewhere:
        out.append(issue("canonical_elsewhere", "Pages point their canonical to a different URL", "Notice", "Canonicals",
                         "Make sure each of these is a true duplicate. If not, set the canonical to the page itself.",
                         ["Read the target URL beside each page.", "If the page has its own content, change the canonical to its own URL.", "Crawl again."], elsewhere, help_url=DOC["canonical"]))
    return out


_WORD_OK = re.compile(r"^[a-z][a-z'\-]{3,}$")


def spelling_issues(html_items: list[dict], ignore: set[str] | None = None) -> list[dict]:
    ignore = {w.lower() for w in (ignore or set())}
    in_heading, in_body = [], []
    for item in html_items:
        meta = item.get("meta") or {}
        spell = meta.get("spell") if isinstance(meta.get("spell"), dict) else {}
        words = []
        for row in spell.get("misspelled") or []:
            word = str((row or {}).get("word") if isinstance(row, dict) else row or "").strip()
            low = word.lower()
            # Skip brand names, acronyms, numbers, very short tokens and site words.
            if not word or word[:1].isupper() or not _WORD_OK.match(low) or low in ignore:
                continue
            if low not in words:
                words.append(low)
        if not words:
            continue
        title = str(meta.get("title") or "")
        htags = meta.get("htags") if isinstance(meta.get("htags"), dict) else {}
        heading_text = " ".join([title] + [str(v) for level in ("h1", "h2", "h3") for v in (htags.get(level) or [])]).lower()
        visible = [w for w in words if re.search(rf"\b{re.escape(w)}\b", heading_text)]
        url = item.get("url") or ""
        if visible:
            in_heading.append(page_row(url, title=title, detail="Possible typo in the title or a heading: " + ", ".join(f"“{w}”" for w in visible[:8])))
        rest = [w for w in words if w not in visible]
        if rest:
            in_body.append(page_row(url, title=title, detail="Possible typos in the text: " + ", ".join(f"“{w}”" for w in rest[:12])))
    out = []
    steps = ["Open the page and search (Ctrl/Cmd+F) for each word listed.", "Correct real typos. Ignore local names, product names and industry terms.",
             "Update the page, then crawl again."]
    if in_heading:
        out.append(issue("typo_heading", "Typos in titles or headings", "Warning", "Spelling",
                         "Fix the spelling in the title or heading. These show in Google and at the top of the page.", steps, in_heading))
    if in_body:
        out.append(issue("typo_text", "Possible typos in page text", "Notice", "Spelling",
                         "Check the words listed for each page and correct real typos.", steps, in_body))
    return out


def heading_duplicate_issues(html_items: list[dict]) -> list[dict]:
    by_h1: dict[str, list[dict]] = {}
    long_h1, no_sections = [], []
    for item in html_items:
        meta = item.get("meta") or {}
        htags = meta.get("htags") if isinstance(meta.get("htags"), dict) else {}
        h1 = [str(v).strip() for v in (htags.get("h1") or []) if str(v).strip()]
        h2 = [str(v).strip() for v in (htags.get("h2") or []) if str(v).strip()]
        url = item.get("url") or ""
        if h1:
            by_h1.setdefault(h1[0].lower(), []).append(page_row(url, title=h1[0], detail=f"H1: “{h1[0]}”"))
            if len(h1[0]) > 70:
                long_h1.append(page_row(url, detail=f"H1 is {len(h1[0])} characters: “{h1[0][:90]}…”. Keep it under about 70."))
        words = ((meta.get("content") or {}) if isinstance(meta.get("content"), dict) else {}).get("plain_text_word_count")
        if h1 and not h2 and isinstance(words, (int, float)) and words >= 400:
            no_sections.append(page_row(url, detail=f"{int(words)} words with no H2 sections. Break the page into H2 sections."))
    dupes = [row for rows in by_h1.values() if len(rows) > 1 for row in rows]
    out = []
    if dupes:
        out.append(issue("duplicate_h1", "Several pages share the same H1", "Warning", "Headings",
                         "Give each page an H1 about its own subject.",
                         ["Group the pages below by H1.", "Rewrite each H1 to name that page's own service, product or place.", "Crawl again."], dupes))
    if long_h1:
        out.append(issue("h1_too_long", "H1 headings are too long", "Notice", "Headings",
                         "Shorten the H1 to one clear phrase.", ["Keep the H1 under about 70 characters.", "Move extra detail into the first paragraph."], long_h1))
    if no_sections:
        out.append(issue("no_h2_sections", "Long pages without H2 sections", "Notice", "Headings",
                         "Split long pages into H2 sections so readers and Google can scan them.",
                         ["Add an H2 above each topic on the page.", "Use H3 under an H2 for sub-points."], no_sections))
    return out


def broken_resource_issues(html_items: list[dict], resources: list[dict]) -> list[dict]:
    pages = [
        page_row(i.get("url") or "", detail="This page loads at least one missing image, stylesheet (CSS) or script.")
        for i in html_items
        if i.get("broken_resources") is True
    ]
    files = []
    for res in resources or []:
        code = res.get("status_code")
        if not isinstance(code, int) or code < 400:
            continue
        kind = {"image": "Image", "stylesheet": "CSS file", "script": "Script"}.get(str(res.get("resource_type")), "File")
        files.append(page_row(str(res.get("url") or ""), status=code, title=kind, detail=f"{kind} returns HTTP {code}."))
    if not pages and not files:
        return []
    return [issue("broken_resources", "Missing images, CSS or script files", "Critical" if any(f["title"] == "CSS file" for f in files) else "Warning",
                  "Assets",
                  "Restore or replace each missing file, or remove the reference from the theme/page.",
                  ["Open the file URL below. A 404 means it was deleted or renamed.",
                   "Missing CSS breaks the layout; missing scripts break sliders, forms and menus; missing images show empty boxes.",
                   "Re-upload the file, update the link in the page/theme, or clear the cache/minify plugin and rebuild.",
                   "Crawl again."], files + pages)]


# ========================================================================================
# 2. Site scan (raw HTML from our reader)
# ========================================================================================

class _AuditReader(HTMLParser):
    ACCORDION = re.compile(r"accordion|faq|collapse|toggle|elementor-tab|wp-block-details|kt-accordion|schema-faq", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.outline: list[list] = []
        self._heading: int | None = None
        self._buf: list[str] = []
        self.json_ld: list[str] = []
        self._in_ld = False
        self._ld: list[str] = []
        self.itemtypes: list[str] = []
        self.accordion = 0
        self.summaries: list[str] = []
        self._in_summary = False
        self._sum: list[str] = []
        self.images: list[dict] = []
        self.empty_headings = 0
        self._skip = 0
        self.text: list[str] = []
        self._chars = 0

    SKIP = {"script", "style", "noscript", "template", "svg"}

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._ld = True, []
            return
        if tag in self.SKIP:
            self._skip += 1
            return
        if a.get("itemtype") and len(self.itemtypes) < 20:
            self.itemtypes.append(a["itemtype"].rstrip("/").rsplit("/", 1)[-1][:60])
        if tag in {"details"} or self.ACCORDION.search(a.get("class", "")) or "aria-expanded" in a:
            self.accordion += 1
        if tag == "summary":
            self._in_summary, self._sum = True, []
        if re.fullmatch(r"h[1-6]", tag or ""):
            self._heading, self._buf = int(tag[1]), []
        if tag == "img" and len(self.images) < 60:
            self.images.append({
                "src": (a.get("src") or a.get("data-src") or "")[:300],
                "w": a.get("width", ""),
                "h": a.get("height", ""),
                "alt": a.get("alt"),
            })

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            if len(self.json_ld) < 10:
                self.json_ld.append("".join(self._ld))
            self._in_ld = False
            return
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "summary" and self._in_summary:
            text = " ".join(" ".join(self._sum).split())
            if text and len(self.summaries) < 30:
                self.summaries.append(text[:180])
            self._in_summary = False
        if self._heading is not None and tag == f"h{self._heading}":
            text = " ".join(" ".join(self._buf).split())
            if text:
                if len(self.outline) < 80:
                    self.outline.append([self._heading, text[:140]])
            else:
                self.empty_headings += 1
            self._heading = None

    def handle_data(self, data):
        if self._in_ld:
            self._ld.append(data or "")
            return
        if self._skip:
            return
        if self._chars < 200_000 and data and data.strip():
            self.text.append(data)
            self._chars += len(data)
        if self._heading is not None:
            self._buf.append(data or "")
        if self._in_summary:
            self._sum.append(data or "")


def analyze_html(html: str, url: str, page_text: str = "") -> dict:
    """Compact per-page audit facts stored on the scan page as page['audit']."""
    reader = _AuditReader()
    try:
        reader.feed(html or "")
    except Exception:  # noqa: BLE001 - broken HTML must never fail a scan
        pass
    # Full visible text (not the short excerpt the scan stores) so price / FAQ checks
    # don't flag content that is simply further down the page.
    text = " ".join(" ".join(reader.text).split()) or page_text or ""
    schema = validate_json_ld(reader.json_ld, text, url)
    questions = [t for _lvl, t in reader.outline if t.endswith("?")] + [s for s in reader.summaries if s.endswith("?")]
    imgs = [i for i in reader.images if i["src"] and not i["src"].startswith("data:")]
    no_dims = [i["src"] for i in imgs if not (i["w"] and i["h"])]
    bad_names = [i["src"] for i in imgs if re.search(r"/(img|image|dsc|photo|screenshot|untitled|wp-image)[-_ ]?\d+|/\d{6,}\.", i["src"], re.I)]
    return {
        "outline": reader.outline,
        "emptyHeadings": reader.empty_headings,
        "schema": schema,
        "microdata": sorted(set(reader.itemtypes))[:10],
        "accordion": reader.accordion,
        "questions": list(dict.fromkeys(questions))[:20],
        "imagesNoSize": no_dims[:10],
        "imagesNoSizeCount": len(no_dims),
        "imageBadNames": bad_names[:10],
        "imageCount": len(imgs),
    }


# --- JSON-LD schema validation -------------------------------------------------------------

def _types(node: dict) -> list[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).rsplit("/", 1)[-1] for v in values if v]


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return float(text)
    return None


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("text") or value.get("@id") or "")
    return str(value or "")


LOCAL_TYPES = re.compile(
    r"(LocalBusiness|Store|Restaurant|Dentist|Physician|LegalService|Attorney|Plumber|Electrician|HVACBusiness|"
    r"RoofingContractor|HomeAndConstructionBusiness|AutoRepair|AutoDealer|RealEstateAgent|InsuranceAgency|"
    r"BeautySalon|HairSalon|DaySpa|HealthClub|Hotel|LodgingBusiness|MedicalClinic|ProfessionalService|"
    r"FinancialService|AccountingService|Locksmith|MovingCompany|CafeOrCoffeeShop|Bakery|Bar)$"
)


class _Finding:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def err(self, group: str, schema_type: str, message: str):
        self.errors.append({"group": group, "type": schema_type, "message": message})

    def warn(self, group: str, schema_type: str, message: str):
        self.warnings.append({"group": group, "type": schema_type, "message": message})


def _check_offer(f: _Finding, offer: dict, owner: str, page_text: str):
    t = _types(offer)
    if "AggregateOffer" in t:
        low, high = _num(offer.get("lowPrice")), _num(offer.get("highPrice"))
        if offer.get("lowPrice") in (None, ""):
            f.err("price", "AggregateOffer", f"{owner}: AggregateOffer is missing lowPrice.")
        elif low is None:
            f.err("price", "AggregateOffer", f"{owner}: lowPrice “{offer.get('lowPrice')}” is not a number (use 49.99, no currency sign).")
        if offer.get("highPrice") not in (None, "") and high is None:
            f.err("price", "AggregateOffer", f"{owner}: highPrice “{offer.get('highPrice')}” is not a number.")
        if low is not None and high is not None and low > high:
            f.err("price", "AggregateOffer", f"{owner}: lowPrice ({offer.get('lowPrice')}) is higher than highPrice ({offer.get('highPrice')}).")
        if low is not None and high is not None and low == 0 and high > 0:
            f.warn("price", "AggregateOffer", f"{owner}: lowPrice is 0. Use the real lowest price, or Google may show “from 0”.")
        if not offer.get("priceCurrency"):
            f.err("price", "AggregateOffer", f"{owner}: AggregateOffer is missing priceCurrency (for example USD, CAD, EUR).")
        if not offer.get("offerCount"):
            f.warn("price", "AggregateOffer", f"{owner}: offerCount is recommended for AggregateOffer.")
        return
    spec = offer.get("priceSpecification") if isinstance(offer.get("priceSpecification"), dict) else {}
    raw_price = offer.get("price", spec.get("price"))
    price = _num(raw_price)
    if raw_price in (None, ""):
        f.err("price", "Offer", f"{owner}: Offer is missing price.")
    elif price is None:
        f.err("price", "Offer", f"{owner}: price “{raw_price}” is not a plain number (write 49.99, put the currency in priceCurrency).")
    elif price < 0:
        f.err("price", "Offer", f"{owner}: price is negative ({raw_price}).")
    elif price == 0:
        f.warn("price", "Offer", f"{owner}: price is 0. Google shows this as free.")
    currency = offer.get("priceCurrency", spec.get("priceCurrency"))
    if not currency:
        f.err("price", "Offer", f"{owner}: Offer is missing priceCurrency (for example USD, CAD, EUR).")
    elif not re.fullmatch(r"[A-Z]{3}", str(currency)):
        f.err("price", "Offer", f"{owner}: priceCurrency “{currency}” must be a 3-letter ISO code like USD.")
    if not offer.get("availability"):
        f.warn("price", "Offer", f"{owner}: availability is recommended (for example https://schema.org/InStock).")
    until = str(offer.get("priceValidUntil") or "")
    if until:
        try:
            if datetime.fromisoformat(until[:10]).date() < datetime.now(timezone.utc).date():
                f.err("price", "Offer", f"{owner}: priceValidUntil ({until[:10]}) is in the past, so Google treats the price as expired.")
        except ValueError:
            f.warn("price", "Offer", f"{owner}: priceValidUntil “{until}” is not a date (use YYYY-MM-DD).")
    if price and page_text:
        shown = re.findall(r"\d[\d,]*(?:\.\d{1,2})?", page_text)
        values = {_num(s.replace(",", "")) for s in shown}
        if price not in values:
            f.warn("price", "Offer", f"{owner}: schema price {raw_price} is not shown on the page. Google requires the price to match what visitors see.")


def _check_rating(f: _Finding, rating: dict, owner: str, *, aggregate: bool):
    kind = "AggregateRating" if aggregate else "Rating"
    value = _num(rating.get("ratingValue"))
    if rating.get("ratingValue") in (None, ""):
        f.err("review", kind, f"{owner}: {kind} is missing ratingValue.")
        return
    if value is None:
        f.err("review", kind, f"{owner}: ratingValue “{rating.get('ratingValue')}” is not a number.")
        return
    best = _num(rating.get("bestRating")) or 5.0
    worst = _num(rating.get("worstRating")) if rating.get("worstRating") not in (None, "") else 1.0
    if value > best or value < (worst or 0):
        f.err("review", kind, f"{owner}: ratingValue {value:g} is outside {worst or 0:g}–{best:g}. Set bestRating/worstRating to match the scale.")
    if aggregate:
        count = next((n for n in (_num(rating.get("reviewCount")), _num(rating.get("ratingCount"))) if n is not None), None)
        if count is None:
            f.err("review", kind, f"{owner}: AggregateRating needs reviewCount or ratingCount.")
        elif count < 1:
            f.err("review", kind, f"{owner}: reviewCount is {count:g}. A rating needs at least one review.")


def _check_review(f: _Finding, review: dict, owner: str, *, standalone: bool):
    rating = review.get("reviewRating")
    if not isinstance(rating, dict):
        f.err("review", "Review", f"{owner}: Review is missing reviewRating with ratingValue.")
    else:
        _check_rating(f, rating, owner, aggregate=False)
    if not _text(review.get("author")).strip():
        f.err("review", "Review", f"{owner}: Review is missing author (a person or organization name).")
    if standalone and not review.get("itemReviewed"):
        f.err("review", "Review", f"{owner}: a stand-alone Review needs itemReviewed (what is being reviewed).")


def _visible(answer: str, page_text: str) -> bool:
    clean = re.sub(r"<[^>]+>", " ", answer or "")
    words = re.findall(r"[a-z0-9]+", clean.lower())
    if not words:
        return False
    probe = " ".join(words[:8])
    body = " ".join(re.findall(r"[a-z0-9]+", (page_text or "").lower()))
    return probe in body


def _check_node(f: _Finding, node: dict, page_text: str, top: bool):
    types = _types(node)
    name = _text(node.get("name") or node.get("headline"))[:60]
    owner = f"{'/'.join(types) or 'Item'}{f' “{name}”' if name else ''}"
    if "FAQPage" in types:
        entities = [e for e in _as_list(node.get("mainEntity")) if isinstance(e, dict)]
        if not entities:
            f.err("faq", "FAQPage", "FAQPage has no mainEntity questions.")
        hidden = 0
        for q in entities:
            qname = _text(q.get("name"))[:80]
            ans = q.get("acceptedAnswer")
            ans = ans[0] if isinstance(ans, list) and ans else ans
            if not qname:
                f.err("faq", "Question", "A Question in FAQPage has no name (the question text).")
            if not isinstance(ans, dict) or not _text(ans.get("text")).strip():
                f.err("faq", "Question", f"Question “{qname or '?'}” has no acceptedAnswer.text.")
            elif page_text and not _visible(_text(ans.get("text")), page_text):
                hidden += 1
        if hidden:
            f.warn("faq", "FAQPage", f"{hidden} FAQ answer(s) in the schema are not visible in the page text. Google requires every answer to be shown on the page.")
    if "Product" in types:
        if not name:
            f.err("price", "Product", "Product is missing name.")
        if not (node.get("offers") or node.get("review") or node.get("aggregateRating")):
            f.err("price", "Product", f"{owner}: Product needs at least one of offers, review or aggregateRating.")
        if not node.get("image"):
            f.warn("price", "Product", f"{owner}: image is recommended.")
        for offer in _as_list(node.get("offers")):
            if isinstance(offer, dict):
                _check_offer(f, offer, owner, page_text)
    if "Offer" in types and top:
        _check_offer(f, node, owner, page_text)
    if "AggregateOffer" in types and top:
        _check_offer(f, node, owner, page_text)
    agg = node.get("aggregateRating")
    if isinstance(agg, dict):
        _check_rating(f, agg, owner, aggregate=True)
    if "AggregateRating" in types and top:
        _check_rating(f, node, owner, aggregate=True)
    for review in _as_list(node.get("review")):
        if isinstance(review, dict):
            _check_review(f, review, owner, standalone=False)
    if "Review" in types and top:
        _check_review(f, node, owner, standalone=True)
    if "BreadcrumbList" in types:
        items = [i for i in _as_list(node.get("itemListElement")) if isinstance(i, dict)]
        if not items:
            f.err("breadcrumb", "BreadcrumbList", "BreadcrumbList has no itemListElement.")
        for idx, item in enumerate(items, 1):
            if _num(item.get("position")) is None:
                f.err("breadcrumb", "ListItem", f"Breadcrumb item {idx} is missing a numeric position.")
            label = _text(item.get("name") or (item.get("item") if isinstance(item.get("item"), dict) else ""))
            if not label:
                f.err("breadcrumb", "ListItem", f"Breadcrumb item {idx} is missing name.")
            if idx < len(items) and not item.get("item"):
                f.err("breadcrumb", "ListItem", f"Breadcrumb item {idx} (“{label or '?'}”) is missing item (its URL).")
    if any(t in {"Article", "BlogPosting", "NewsArticle"} for t in types):
        headline = _text(node.get("headline"))
        if not headline:
            f.warn("article", "Article", "Article is missing headline.")
        elif len(headline) > 110:
            f.warn("article", "Article", f"Article headline is {len(headline)} characters. Keep it under 110.")
        for prop in ("image", "datePublished", "author"):
            if not node.get(prop):
                f.warn("article", "Article", f"{owner}: {prop} is recommended.")
    if any(LOCAL_TYPES.search(t) for t in types):
        if not name:
            f.err("business", "LocalBusiness", f"{'/'.join(types)} is missing name.")
        address = node.get("address")
        if not address:
            f.err("business", "LocalBusiness", f"{owner}: address is required for a local business.")
        elif isinstance(address, dict):
            for prop in ("streetAddress", "addressLocality", "postalCode", "addressCountry"):
                if not address.get(prop):
                    f.warn("business", "PostalAddress", f"{owner}: address.{prop} is recommended.")
        for prop in ("telephone", "url", "openingHoursSpecification"):
            if not node.get(prop) and not (prop == "openingHoursSpecification" and node.get("openingHours")):
                f.warn("business", "LocalBusiness", f"{owner}: {prop} is recommended.")
    if "Organization" in types and top:
        if not name:
            f.warn("business", "Organization", "Organization is missing name.")
        if not node.get("logo"):
            f.warn("business", "Organization", f"{owner}: logo is recommended.")
    if "VideoObject" in types:
        for prop in ("name", "thumbnailUrl", "uploadDate"):
            if not node.get(prop):
                f.err("video", "VideoObject", f"{owner}: {prop} is required for video results.")
        if not node.get("description"):
            f.warn("video", "VideoObject", f"{owner}: description is recommended.")
    if "ImageObject" in types and not (node.get("contentUrl") or node.get("url")):
        f.err("image", "ImageObject", "ImageObject is missing contentUrl (the image file URL).")
    if "Event" in types:
        for prop in ("name", "startDate", "location"):
            if not node.get(prop):
                f.err("other", "Event", f"{owner}: {prop} is required.")
    if "HowTo" in types:
        f.warn("other", "HowTo", "HowTo rich results are no longer shown by Google. The markup is harmless but brings no rich result.")
    for value in node.values():
        for child in _as_list(value):
            if isinstance(child, dict) and child.get("@type") and child is not node:
                if child.get("@type") in ("Offer", "AggregateOffer", "Review", "Rating", "AggregateRating", "PostalAddress", "ListItem", "Question", "Answer"):
                    continue
                _check_node(f, child, page_text, top=False)


def validate_json_ld(blocks: list[str], page_text: str = "", url: str = "") -> dict:
    """{types, errors[{group,type,message}], warnings[...], invalid: n}"""
    f = _Finding()
    types: list[str] = []
    invalid = 0
    for raw in blocks or []:
        text = (raw or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            invalid += 1
            f.err("syntax", "JSON-LD", f"A JSON-LD block is not valid JSON ({str(exc)[:80]}). Google ignores the whole block.")
            continue
        roots = data if isinstance(data, list) else [data]
        for root in roots:
            if not isinstance(root, dict):
                continue
            context = str(root.get("@context") or "")
            nodes = [n for n in _as_list(root.get("@graph")) if isinstance(n, dict)] or [root]
            if not context and not root.get("@graph"):
                f.err("syntax", "JSON-LD", "A JSON-LD block has no @context (use \"https://schema.org\").")
            elif context and "schema.org" not in context:
                f.warn("syntax", "JSON-LD", f"@context is “{context[:60]}”, not schema.org.")
            for node in nodes:
                if not node.get("@type"):
                    continue
                types.extend(_types(node))
                _check_node(f, node, page_text, top=True)
    return {"types": sorted(set(types))[:20], "errors": f.errors[:40], "warnings": f.warnings[:40], "invalid": invalid}


SCHEMA_GROUPS = {
    "syntax": ("Schema code is broken (invalid JSON-LD)", "Critical", "schema_validator"),
    "price": ("Product price schema errors (price, lowest / highest price)", "Critical", "product"),
    "review": ("Review and rating schema errors", "Critical", "review"),
    "faq": ("FAQ schema errors", "Warning", "faq"),
    "breadcrumb": ("Breadcrumb schema errors", "Warning", "breadcrumb"),
    "business": ("Business / organization schema errors", "Warning", "business"),
    "article": ("Article / blog schema problems", "Notice", "article"),
    "video": ("Video schema errors", "Warning", "video"),
    "image": ("Image schema errors", "Warning", "images"),
    "other": ("Other schema problems", "Notice", "schema_validator"),
}

SCHEMA_STEPS = [
    "Open the page in Google's Rich Results Test (button below) to see the same error.",
    "Find where the schema comes from: your SEO plugin (Yoast/Rank Math schema tab), a schema/reviews/WooCommerce plugin, or code in the theme.",
    "Fix the field named in the message (for example add priceCurrency, or make lowPrice lower than highPrice).",
    "Re-test in the Rich Results Test, then crawl again.",
]


# ========================================================================================
# 3. Issues from a stored site scan
# ========================================================================================

def _outline_problems(outline: list[list]) -> list[str]:
    notes: list[str] = []
    if not outline:
        return notes
    levels = [int(lvl) for lvl, _ in outline]
    h1s = [text for lvl, text in outline if int(lvl) == 1]
    if not h1s:
        notes.append("No H1 on the page.")
    elif len(h1s) > 1:
        notes.append(f"{len(h1s)} H1 headings: " + "; ".join(f"“{t[:50]}”" for t in h1s[:3]) + ". Keep one H1.")
    if h1s and levels[0] != 1:
        first_lvl, first_text = outline[0]
        notes.append(f"The first heading is H{first_lvl} “{first_text[:50]}”, before the H1.")
    # Walk in document order, remembering the current section at each level so a skip
    # is reported with the section it happens in.
    section = {1: "", 2: "", 3: "", 4: "", 5: "", 6: ""}
    prev = 0
    for lvl, text in outline:
        lvl = int(lvl)
        if prev and lvl - prev > 1:
            parent = section.get(prev) or ""
            where = f" in section “{parent[:50]}”" if parent else ""
            notes.append(f"H{prev} → H{lvl} “{text[:50]}”{where}: H{prev + 1} is skipped.")
            if len(notes) >= 8:
                break
        section[lvl] = text
        for deeper in range(lvl + 1, 7):
            section[deeper] = ""
        prev = lvl
    return notes


def scan_issues(scan: dict | None) -> list[dict]:
    """Audit issues from a stored site scan (pages that carry page['audit'])."""
    scan = scan or {}
    pages = [p for p in (scan.get("pages") or []) if isinstance(p.get("audit"), dict)]
    out: list[dict] = []
    outline_rows, empty_rows, accordion_rows, no_size_rows, bad_name_rows = [], [], [], [], []
    schema_rows: dict[str, list[dict]] = {}
    schema_warn_rows: list[dict] = []
    schema_types: dict[str, int] = {}
    for page in pages:
        audit = page["audit"]
        url = page.get("url") or ""
        outline = audit.get("outline") or []
        problems = _outline_problems(outline)
        if problems:
            seq = " ".join(f"H{lvl}" for lvl, _ in outline[:24])
            outline_rows.append(page_row(url, title=page.get("title") or "", detail=" ".join(problems) + f" Order on the page: {seq}{' …' if len(outline) > 24 else ''}."))
        if audit.get("emptyHeadings"):
            empty_rows.append(page_row(url, detail=f"{audit['emptyHeadings']} empty heading tag(s). Remove them or add text."))
        schema = audit.get("schema") or {}
        for t in schema.get("types") or []:
            schema_types[t] = schema_types.get(t, 0) + 1
        grouped: dict[str, list[str]] = {}
        for err in schema.get("errors") or []:
            grouped.setdefault(err.get("group") or "other", []).append(err.get("message") or "")
        for group, messages in grouped.items():
            schema_rows.setdefault(group, []).append(page_row(url, title=page.get("title") or "", detail=" • ".join(dict.fromkeys(messages))[:600]))
        warns = [w.get("message") or "" for w in schema.get("warnings") or []]
        if warns:
            schema_warn_rows.append(page_row(url, detail=" • ".join(dict.fromkeys(warns))[:600]))
        has_faq = "FAQPage" in (schema.get("types") or [])
        questions = audit.get("questions") or []
        if audit.get("accordion") and len(questions) >= 2 and not has_faq:
            accordion_rows.append(page_row(url, detail=f"Accordion/toggle with {len(questions)} questions and no FAQPage schema: " + "; ".join(f"“{q[:60]}”" for q in questions[:3])))
        if audit.get("imagesNoSizeCount"):
            no_size_rows.append(page_row(url, detail=f"{audit['imagesNoSizeCount']} image(s) without width/height, e.g. {(audit.get('imagesNoSize') or [''])[0]}"))
        if audit.get("imageBadNames"):
            bad_name_rows.append(page_row(url, detail="File names that say nothing about the image: " + ", ".join(s.rsplit('/', 1)[-1] for s in audit["imageBadNames"][:4])))
    if outline_rows:
        out.append(issue("heading_sections", "Heading order by section (H1–H6)", "Warning", "Headings",
                         "Use one H1, then H2 for each section and H3 under H2 — never skip a level.",
                         ["Open the page and read the headings in order (the order is shown beside each page).",
                          "In the editor, change the heading level of the item named (for example H4 → H3).",
                          "Change the level only. If it is styled bigger/smaller, adjust the style, not the tag.",
                          "Update and scan again."], outline_rows, source="scan"))
    if empty_rows:
        out.append(issue("empty_headings", "Empty heading tags", "Notice", "Headings",
                         "Remove empty heading blocks or put text in them.",
                         ["Look for heading blocks with no text (often spacer or icon blocks).", "Delete them or use a normal block instead."], empty_rows, source="scan"))
    for group, rows in schema_rows.items():
        name, severity, doc = SCHEMA_GROUPS.get(group, SCHEMA_GROUPS["other"])
        steps = list(SCHEMA_STEPS)
        if group == "faq":
            steps.append("Note: Google now shows FAQ rich results mainly for well-known government and health sites; valid FAQ schema still helps other search and AI tools.")
        out.append(issue(f"schema_{group}", name, severity, "Schema",
                         "Fix the schema field named for each page so Google can show the rich result.", steps, rows,
                         help_url=DOC[doc], source="scan"))
    if schema_warn_rows:
        out.append(issue("schema_recommended", "Schema is missing recommended fields", "Notice", "Schema",
                         "Add the recommended fields listed for each page. They are optional but improve rich results.",
                         SCHEMA_STEPS, schema_warn_rows, help_url=DOC["rich_test"], source="scan"))
    if accordion_rows:
        out.append(issue("accordion_without_faq", "FAQ accordions without FAQ schema", "Notice", "FAQ",
                         "Add FAQPage schema that uses the same questions and answers shown in the accordion.",
                         ["Use your SEO plugin's FAQ block (Yoast FAQ / Rank Math FAQ) instead of a plain accordion, or add FAQPage JSON-LD.",
                          "Use the exact question and answer text that is visible on the page.",
                          "Test the page in the Rich Results Test."], accordion_rows, help_url=DOC["faq"], source="scan"))
    if no_size_rows:
        out.append(issue("images_no_dimensions", "Images without width and height", "Notice", "Images",
                         "Set width and height on images so the page doesn't jump while loading (layout shift / CLS).",
                         ["Insert images through the editor's image block, which adds width/height.", "For theme images, ask the developer to add width and height attributes."],
                         no_size_rows, help_url=DOC["images"], source="scan"))
    if bad_name_rows:
        out.append(issue("image_file_names", "Image file names don't describe the image", "Notice", "Images",
                         "Rename new uploads to short descriptive names (for example roof-repair-boston.jpg).",
                         ["Rename the file before uploading it next time.", "For existing images, a rename plugin can update the file name and links."],
                         bad_name_rows, help_url=DOC["images"], source="scan"))
    broken = {}
    for row in scan.get("failed") or []:
        status = row.get("status")
        if isinstance(status, int) and status >= 400:
            broken.setdefault(status, []).append(page_row(str(row.get("url") or ""), status=status, detail=f"Linked inside the site but returns HTTP {status} {STATUS_TEXT.get(status, '')}."))
    for status, rows in sorted(broken.items()):
        fix, steps = STATUS_FIX.get(status) or STATUS_FIX[404 if status < 500 else 500]
        out.append(issue(f"scan_broken_{status}", f"Internal links lead to {status} {STATUS_TEXT.get(status, 'errors')}",
                         "Critical", "Links", fix, steps, rows, help_url=DOC["status"], source="scan"))
    out.extend(site_issues(scan.get("siteChecks") or {}))
    out.sort(key=lambda row: (SEVERITY_ORDER.get(row["severity"], 3), -row["count"]))
    return out


# ========================================================================================
# 4. Site checks: robots.txt, sitemap, redirects (network, run once per scan)
# ========================================================================================

KNOWN_DIRECTIVES = {"user-agent", "disallow", "allow", "sitemap", "crawl-delay", "host", "noindex", "clean-param"}
RENDER_PATHS = re.compile(r"^/(wp-content|wp-includes|assets|static|_next|css|js)(/|$)|\*\.(css|js)\b|\.css\$?$|\.js\$?$", re.I)


def _robots_groups(lines: list[str]) -> list[tuple[list[str], list[tuple[str, str]]]]:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in {"allow", "disallow"}:
            rules.append((key, value))
    if agents or rules:
        groups.append((agents, rules))
    return groups


def check_robots(text: str, status: int | None, size: int = 0) -> dict:
    """Pure robots.txt analysis -> {status, sitemaps[], problems[{key,severity,detail}]}"""
    problems: list[dict] = []
    if status is None:
        problems.append({"key": "robots_unreachable", "severity": "Warning", "detail": "robots.txt could not be loaded (network error or timeout)."})
        return {"status": None, "sitemaps": [], "problems": problems}
    if status >= 500:
        problems.append({"key": "robots_5xx", "severity": "Critical",
                         "detail": f"robots.txt returns HTTP {status}. Google treats a server error on robots.txt as 'do not crawl' and may stop crawling the whole site."})
        return {"status": status, "sitemaps": [], "problems": problems}
    if status >= 400:
        problems.append({"key": "robots_missing", "severity": "Notice", "detail": f"robots.txt returns HTTP {status}. Google crawls everything, but there is no place to list the sitemap."})
        return {"status": status, "sitemaps": [], "problems": problems}
    lines = text.splitlines()
    sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.strip().lower().startswith("sitemap:")]
    if size > 500 * 1024:
        problems.append({"key": "robots_too_big", "severity": "Warning", "detail": f"robots.txt is {size // 1024} KB. Google reads only the first 500 KB."})
    bad = []
    for number, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            bad.append(f"line {number}: “{line[:50]}” has no colon")
            continue
        key = line.split(":", 1)[0].strip().lower()
        if key not in KNOWN_DIRECTIVES:
            bad.append(f"line {number}: unknown rule “{key[:30]}”")
        elif key == "noindex":
            problems.append({"key": "robots_noindex", "severity": "Notice", "detail": f"line {number}: Google ignores 'Noindex' in robots.txt. Use a noindex meta tag on the page instead."})
        elif key == "crawl-delay":
            problems.append({"key": "robots_crawl_delay", "severity": "Notice", "detail": f"line {number}: Google ignores Crawl-delay."})
    if bad:
        problems.append({"key": "robots_syntax", "severity": "Notice", "detail": "robots.txt syntax: " + "; ".join(bad[:6])})
    for agents, rules in _robots_groups(lines):
        if not any(a in {"*", "googlebot"} for a in agents):
            continue
        who = "all crawlers" if "*" in agents else "Googlebot"
        disallow = [v for k, v in rules if k == "disallow" and v]
        allow = [v for k, v in rules if k == "allow" and v]
        if "/" in disallow and not any(a in {"/", "/*"} for a in allow):
            problems.append({"key": "robots_blocks_site", "severity": "Critical", "detail": f"“Disallow: /” for {who} blocks the whole website from Google."})
        blocked_assets = [v for v in disallow if RENDER_PATHS.search(v)]
        if blocked_assets:
            problems.append({"key": "robots_blocks_assets", "severity": "Warning",
                             "detail": f"Rules block files Google needs to render pages for {who}: " + ", ".join(blocked_assets[:5])})
    if not sitemaps:
        problems.append({"key": "robots_no_sitemap", "severity": "Notice", "detail": "robots.txt has no 'Sitemap:' line. Add the sitemap URL so crawlers find every page."})
    return {"status": status, "sitemaps": sitemaps[:10], "problems": problems}


def run_site_checks(home: str, sample_urls: list[str] | None = None, timeout: float = 6.0) -> dict:
    """Network checks for robots.txt, sitemap and redirects. Never raises."""
    import httpx
    from urllib.robotparser import RobotFileParser

    from app.site_scan import AGENT, public_host

    result: dict = {"robots": {}, "sitemap": {}, "redirects": {}, "checkedAt": datetime.utcnow().isoformat(timespec="seconds")}
    try:
        parts = urlparse(home if "://" in home else f"https://{home}")
        host = (parts.hostname or "").lower()
        if not host or not public_host(f"https://{host}/"):
            return result
        base = f"https://{host}"
        with httpx.Client(headers={"User-Agent": AGENT}, timeout=timeout) as client:
            # robots.txt
            try:
                r = client.get(f"{base}/robots.txt", follow_redirects=True)
                robots = check_robots(r.text[:600_000] if r.status_code < 400 else "", r.status_code, len(r.content))
                if r.status_code < 400:
                    parser = RobotFileParser()
                    parser.parse(r.text.splitlines())
                    blocked = [u for u in [f"{base}/", *(sample_urls or [])[:15]] if not parser.can_fetch("Googlebot", u)]
                    if blocked:
                        robots["problems"].append({"key": "robots_blocks_pages", "severity": "Critical",
                                                   "detail": "Googlebot is blocked from: " + ", ".join(blocked[:5])})
            except httpx.HTTPError:
                robots = check_robots("", None)
            result["robots"] = robots
            # sitemap
            result["sitemap"] = _check_sitemap(client, robots.get("sitemaps") or [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", f"{base}/wp-sitemap.xml"], host)
            # http -> https and www consistency
            result["redirects"] = _check_redirects(client, host)
    except Exception as exc:  # noqa: BLE001 - site checks are best-effort
        result["error"] = f"{type(exc).__name__}"
    return result


def _hops(client, url: str, limit: int = 6) -> list[tuple[int | None, str]]:
    import httpx

    out: list[tuple[int | None, str]] = []
    current = url
    for _ in range(limit):
        try:
            r = client.get(current, follow_redirects=False)
        except httpx.HTTPError:
            out.append((None, current))
            return out
        out.append((r.status_code, current))
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            current = urljoin(current, r.headers["location"])
            continue
        return out
    return out


def _check_redirects(client, host: str) -> dict:
    bare = host.removeprefix("www.")
    problems: list[dict] = []
    chains = {}
    for start in (f"http://{bare}/", f"http://www.{bare}/", f"https://{bare}/", f"https://www.{bare}/"):
        chains[start] = _hops(client, start)
    finals = {start: hops[-1][1] for start, hops in chains.items() if hops and hops[-1][0] and hops[-1][0] < 400}
    http_hops = chains.get(f"http://{host}/") or chains.get(f"http://{bare}/") or []
    if http_hops and http_hops[0][0] == 200:
        problems.append({"key": "http_no_redirect", "severity": "Warning", "detail": f"http://{host}/ loads without redirecting to https. Turn on 'Always use HTTPS'."})
    elif http_hops and http_hops[0][0] in (302, 307):
        problems.append({"key": "http_temp_redirect", "severity": "Notice", "detail": f"http://{host}/ uses a {http_hops[0][0]} (temporary) redirect to https. Use 301."})
    distinct = {page.rstrip("/").replace("http://", "https://") for page in finals.values()}
    if len(distinct) > 1:
        problems.append({"key": "www_duplicate", "severity": "Warning",
                         "detail": "www and non-www (or http and https) end on different URLs: " + ", ".join(sorted(distinct)[:4]) + ". Redirect all versions to one."})
    for start, hops in chains.items():
        redirects = [h for h in hops if h[0] in (301, 302, 303, 307, 308)]
        if len(redirects) >= 2:
            path = " → ".join(u for _, u in hops)
            problems.append({"key": "home_redirect_chain", "severity": "Notice", "detail": f"{len(redirects)} redirects before the home page loads: {path}"})
            break
    return {"chains": {k: [[s, u] for s, u in v] for k, v in chains.items()}, "problems": problems}


def _check_sitemap(client, candidates: list[str], host: str) -> dict:
    import xml.etree.ElementTree as ET

    import httpx

    problems: list[dict] = []
    bare = host.removeprefix("www.")
    found = ""
    urls: list[str] = []
    for url in candidates[:4]:
        if _host(url) != bare:
            problems.append({"key": "sitemap_other_host", "severity": "Warning", "detail": f"Sitemap {url} is on another domain. Google only accepts sitemaps for the same site unless verified."})
            continue
        try:
            r = client.get(url, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if r.status_code >= 400:
            if url in candidates[:1] and len(candidates) <= 3 and "robots" not in url:
                problems.append({"key": "sitemap_error", "severity": "Warning", "detail": f"Sitemap {url} returns HTTP {r.status_code}."})
            continue
        try:
            root = ET.fromstring(r.content[:5_000_000])
        except ET.ParseError as exc:
            problems.append({"key": "sitemap_invalid", "severity": "Warning", "detail": f"Sitemap {url} is not valid XML ({str(exc)[:60]})."})
            continue
        found = url
        locs = [n.text.strip() for n in root.iter() if n.tag.lower().endswith("loc") and n.text]
        if root.tag.lower().endswith("sitemapindex") and locs:
            try:
                child = ET.fromstring(client.get(locs[0], follow_redirects=True).content[:5_000_000])
                locs = [n.text.strip() for n in child.iter() if n.tag.lower().endswith("loc") and n.text]
            except Exception:  # noqa: BLE001
                locs = []
        urls = locs
        break
    if not found and not any(p["key"] == "sitemap_error" for p in problems):
        problems.append({"key": "sitemap_missing", "severity": "Warning", "detail": "No XML sitemap was found (robots.txt, /sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml)."})
    from concurrent.futures import ThreadPoolExecutor

    def probe(loc: str):
        if _host(loc) != bare:
            return (loc, "other domain")
        try:
            r = client.get(loc, follow_redirects=False)
        except httpx.HTTPError:
            return None
        if r.status_code in (301, 302, 307, 308):
            return (loc, f"redirects ({r.status_code})")
        if r.status_code >= 400:
            return (loc, f"HTTP {r.status_code}")
        if "noindex" in (r.headers.get("x-robots-tag") or "").lower() or re.search(r"<meta[^>]+noindex", r.text[:20000], re.I):
            return (loc, "noindex")
        return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        bad = [hit for hit in pool.map(probe, urls[:15]) if hit]
    if bad:
        problems.append({"key": "sitemap_bad_urls", "severity": "Warning",
                         "detail": "The sitemap lists URLs Google should not index: " + "; ".join(f"{u} ({why})" for u, why in bad[:6]),
                         "urls": [{"url": u, "why": why} for u, why in bad[:15]]})
    return {"url": found, "urlCount": len(urls), "problems": problems}


SITE_PROBLEM_TEXT = {
    "robots_5xx": ("robots.txt returns a server error", "Robots", "robots",
                   ["Open yoursite/robots.txt in a browser.", "Fix the server error with your host (it should return 200 or 404).", "Re-check in Search Console → Settings → robots.txt."]),
    "robots_blocks_site": ("robots.txt blocks the whole website", "Robots", "robots",
                           ["Edit robots.txt (Yoast → Tools → File editor, or your host's file manager).", "Remove “Disallow: /” for User-agent: * / Googlebot.", "In WordPress also check Settings → Reading → 'Discourage search engines' is OFF."]),
    "robots_blocks_pages": ("robots.txt blocks important pages", "Robots", "robots",
                            ["Find the Disallow rule that matches the pages listed.", "Remove or narrow it.", "Test in Search Console → URL Inspection."]),
    "robots_blocks_assets": ("robots.txt blocks CSS/JS needed to render pages", "Robots", "robots",
                             ["Remove Disallow rules for /wp-content/, /wp-includes/, .css and .js.", "Google must load styles and scripts to see the page like a visitor."]),
    "robots_unreachable": ("robots.txt could not be loaded", "Robots", "robots", ["Open yoursite/robots.txt. If it times out, check the server or firewall with your host."]),
    "robots_too_big": ("robots.txt is too large", "Robots", "robots_create", ["Remove repeated or unused rules so the file is under 500 KB."]),
    "robots_syntax": ("robots.txt has syntax problems", "Robots", "robots_create", ["Fix the lines listed: each rule is 'Name: value' on its own line.", "Remove unknown rules."]),
    "robots_noindex": ("robots.txt uses unsupported 'Noindex'", "Robots", "noindex", ["Remove the Noindex line.", "Add a noindex meta tag to the page instead (SEO plugin → Advanced)."]),
    "robots_crawl_delay": ("robots.txt uses Crawl-delay (ignored by Google)", "Robots", "robots", ["You can leave it; Google ignores it. Other crawlers may slow down."]),
    "robots_missing": ("No robots.txt file", "Robots", "robots_create", ["Optional but useful: add a robots.txt with your 'Sitemap:' line.", "Most SEO plugins create one automatically."]),
    "robots_no_sitemap": ("robots.txt doesn't list the sitemap", "Sitemap", "sitemap", ["Add a line: Sitemap: https://yoursite/sitemap_index.xml (your real sitemap URL)."]),
    "sitemap_missing": ("No XML sitemap found", "Sitemap", "sitemap", ["Turn on the XML sitemap in your SEO plugin (Yoast/Rank Math → Sitemap).", "Submit it in Search Console → Sitemaps."]),
    "sitemap_error": ("The sitemap returns an error", "Sitemap", "sitemap", ["Open the sitemap URL in a browser.", "Re-save permalinks (Settings → Permalinks) and the SEO plugin sitemap settings.", "Resubmit in Search Console."]),
    "sitemap_invalid": ("The sitemap is not valid XML", "Sitemap", "sitemap", ["A plugin or theme is printing text before the XML. Disable plugins one at a time to find it.", "Resubmit in Search Console."]),
    "sitemap_other_host": ("The sitemap is on another domain", "Sitemap", "sitemap", ["List the sitemap on this site's own domain."]),
    "sitemap_bad_urls": ("The sitemap lists broken, redirected or noindex URLs", "Sitemap", "sitemap",
                         ["Remove the URLs listed from the sitemap (exclude them in the SEO plugin, or fix the page).", "A sitemap should only list live, indexable, final URLs.", "Resubmit in Search Console."]),
    "http_no_redirect": ("http doesn't redirect to https", "Redirects", "redirects", ["Turn on 'Always use HTTPS' (Cloudflare) or the host's force-HTTPS option.", "Or add a 301 rule from http:// to https://."]),
    "http_temp_redirect": ("http → https uses a temporary redirect", "Redirects", "redirects", ["Change the http → https redirect to 301."]),
    "www_duplicate": ("www and non-www versions both load", "Redirects", "canonical", ["Pick one version (www or not).", "301-redirect the other version to it at the host/CDN.", "Use the same version in WordPress Settings → General."]),
    "home_redirect_chain": ("The home page goes through several redirects", "Redirects", "redirects", ["Make http://, www and non-www all redirect once, straight to the final https home URL."]),
}


def site_issues(checks: dict) -> list[dict]:
    out = []
    for section in ("robots", "sitemap", "redirects"):
        for problem in (checks.get(section) or {}).get("problems") or []:
            key = problem.get("key") or ""
            name, area, doc, steps = SITE_PROBLEM_TEXT.get(key, (key.replace("_", " ").capitalize(), "Technical", "robots", ["Fix it, then scan again."]))
            pages = [page_row(u["url"], detail=u["why"]) for u in problem.get("urls") or []] or [page_row("", detail=problem.get("detail") or "")]
            item = issue(key, name, problem.get("severity") or "Notice", area, problem.get("detail") or "", steps, pages, help_url=DOC.get(doc, ""), source="site")
            item["fix"] = problem.get("detail") or ""
            out.append(item)
    return out


# ========================================================================================
# 5. Google Search Console URL Inspection
# ========================================================================================

COVERAGE_HELP = [
    (re.compile(r"not found|404", re.I), "Critical", "Search Console: page not found (404)",
     ["Restore the page or 301-redirect it to the closest live page.", "Remove it from the sitemap and internal links.", "In Search Console → Pages, click 'Validate fix'."]),
    (re.compile(r"server error|5xx", re.I), "Critical", "Search Console: server error (5xx)",
     ["Fix the server error (host/developer).", "In Search Console → Pages → Server error (5xx), click 'Validate fix'."]),
    (re.compile(r"soft 404", re.I), "Warning", "Search Console: soft 404",
     ["The page loads but looks empty or like an error page to Google.", "Add real content, or return a real 404/410, or redirect it.", "Validate fix in Search Console."]),
    (re.compile(r"redirect error", re.I), "Critical", "Search Console: redirect error",
     ["Follow the URL: there is a loop, a very long chain, or a redirect to a broken/too long URL.", "Make it redirect once to a live page.", "Validate fix."]),
    (re.compile(r"page with redirect", re.I), "Notice", "Search Console: page with redirect (not indexed, expected)",
     ["This URL redirects, so Google indexes the target instead. Remove it from the sitemap and update internal links to the final URL."]),
    (re.compile(r"noindex", re.I), "Warning", "Search Console: excluded by noindex",
     ["If this page should rank, remove the noindex (SEO plugin → Advanced).", "Request indexing in Search Console → URL Inspection."]),
    (re.compile(r"blocked by robots", re.I), "Warning", "Search Console: blocked by robots.txt",
     ["Remove the Disallow rule that matches this URL if it should rank.", "Request indexing."]),
    (re.compile(r"unauthori[sz]ed|401|forbidden|403|other 4xx", re.I), "Critical", "Search Console: blocked (401/403/4xx)",
     ["Google is refused access. Check login protection, firewall or CDN bot rules.", "Validate fix."]),
    (re.compile(r"duplicate.*google chose different", re.I), "Warning", "Search Console: Google chose a different canonical",
     ["Google thinks another URL is the main version (shown beside the page).", "Make the content clearly unique, or set the canonical to the URL Google picked.", "Make internal links and the sitemap use one version."]),
    (re.compile(r"duplicate without user-selected canonical", re.I), "Warning", "Search Console: duplicate without a canonical",
     ["Add a canonical tag pointing to the main version of this content.", "Merge near-identical pages."]),
    (re.compile(r"alternate page with proper canonical", re.I), "Notice", "Search Console: alternate page (canonical elsewhere, expected)",
     ["Nothing to fix if this is intentional (for example a filtered or tracking URL)."]),
    (re.compile(r"crawled.*not indexed", re.I), "Warning", "Search Console: crawled – currently not indexed",
     ["Google read the page but didn't think it worth indexing yet.", "Add more unique, useful content and internal links from strong pages.", "Request indexing after improving it."]),
    (re.compile(r"discovered.*not indexed", re.I), "Notice", "Search Console: discovered – currently not indexed",
     ["Google knows the URL but hasn't crawled it. Add internal links to it and keep it in the sitemap.", "Check the server isn't slow or rate-limiting Google."]),
    (re.compile(r"unknown to google", re.I), "Notice", "Search Console: URL is unknown to Google",
     ["Add the page to the sitemap and link to it from another page.", "Use 'Request indexing' in Search Console → URL Inspection (this can't be done through the API)."]),
]

FETCH_STATE = {
    "SOFT_404": "Google saw a soft 404.", "BLOCKED_ROBOTS_TXT": "Blocked by robots.txt.", "NOT_FOUND": "Not found (404).",
    "ACCESS_DENIED": "Access denied (401).", "SERVER_ERROR": "Server error (5xx).", "REDIRECT_ERROR": "Redirect error.",
    "ACCESS_FORBIDDEN": "Access forbidden (403).", "BLOCKED_4XX": "Blocked by a 4xx error.", "INTERNAL_CRAWL_ERROR": "Google internal crawl error.",
    "INVALID_URL": "Invalid URL.",
}


def inspection_issues(results: list[dict]) -> list[dict]:
    """Issues from URL Inspection results [{url, result | error}]."""
    by_state: dict[str, tuple[str, list[str], list[dict]]] = {}
    canon_rows, rich_err, rich_warn, failed_rows = [], [], [], []
    for row in results or []:
        url = row.get("url") or ""
        if row.get("error"):
            failed_rows.append(page_row(url, detail=str(row["error"])[:200]))
            continue
        res = (row.get("result") or {}).get("inspectionResult") or {}
        idx = res.get("indexStatusResult") or {}
        state = str(idx.get("coverageState") or "")
        verdict = idx.get("verdict")
        crawled = str(idx.get("lastCrawlTime") or "")[:10]
        g_canon, u_canon = str(idx.get("googleCanonical") or ""), str(idx.get("userCanonical") or "")
        mismatch = bool(g_canon and u_canon and g_canon.rstrip("/") != u_canon.rstrip("/"))
        if mismatch:
            canon_rows.append(page_row(url, detail=f"Your canonical: {u_canon} • Google chose: {g_canon}"))
        # The canonical-mismatch finding below already explains "Google chose a different canonical".
        if state and verdict != "PASS" and not (mismatch and re.search(r"google chose different", state, re.I)):
            for pattern, severity, title, steps in COVERAGE_HELP:
                if pattern.search(state):
                    break
            else:
                severity, title, steps = "Warning", f"Search Console: {state}", ["Open the URL in Search Console → URL Inspection for details."]
            fetch = FETCH_STATE.get(str(idx.get("pageFetchState") or ""), "")
            detail = f"Google says: “{state}”." + (f" {fetch}" if fetch else "") + (f" Last crawled {crawled}." if crawled else " Not crawled yet.")
            sev_prev, _steps, rows = by_state.get(title, (severity, steps, []))
            rows.append(page_row(url, detail=detail))
            by_state[title] = (severity, steps, rows)
        rich = res.get("richResultsResult") or {}
        for detected in rich.get("detectedItems") or []:
            kind = detected.get("richResultType") or "Rich result"
            for item in detected.get("items") or []:
                errors = [i.get("issueMessage") for i in item.get("issues") or [] if i.get("severity") == "ERROR"]
                warns = [i.get("issueMessage") for i in item.get("issues") or [] if i.get("severity") == "WARNING"]
                label = f"{kind}{' “' + item['name'][:40] + '”' if item.get('name') else ''}"
                if errors:
                    rich_err.append(page_row(url, title=kind, detail=f"{label}: " + "; ".join(dict.fromkeys(e for e in errors if e))))
                if warns:
                    rich_warn.append(page_row(url, title=kind, detail=f"{label}: " + "; ".join(dict.fromkeys(w for w in warns if w))))
    out = []
    for title, (severity, steps, rows) in by_state.items():
        out.append(issue("gsc_" + re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50], title, severity, "Search Console",
                         "Google's own report for these URLs (URL Inspection). Fix the cause, then use 'Validate fix' or 'Request indexing' in Search Console.",
                         steps, rows, help_url=DOC["page_indexing"], source="gsc"))
    if canon_rows:
        out.append(issue("gsc_canonical_mismatch", "Search Console: Google chose a different canonical than yours", "Warning", "Canonicals",
                         "Google ignores your canonical on these pages. Make the signals agree: canonical, internal links, sitemap and redirects should all use one URL.",
                         ["Compare the two URLs shown for each page.", "If Google's choice is right, change your canonical to it.",
                          "If yours is right, point internal links and the sitemap to it and make the duplicate redirect or differ clearly."],
                         canon_rows, help_url=DOC["canonical"], source="gsc"))
    if rich_err:
        out.append(issue("gsc_rich_result_errors", "Search Console: schema (rich result) errors Google found", "Critical", "Schema",
                         "Google can't show rich results (FAQ, product, review, breadcrumb …) for these items until the errors are fixed.",
                         SCHEMA_STEPS, rich_err, help_url=DOC["rich_test"], source="gsc"))
    if rich_warn:
        out.append(issue("gsc_rich_result_warnings", "Search Console: schema (rich result) warnings", "Notice", "Schema",
                         "Optional fields Google recommends for these rich results.", SCHEMA_STEPS, rich_warn, help_url=DOC["rich_test"], source="gsc"))
    if failed_rows:
        out.append(issue("gsc_inspection_failed", "Search Console could not inspect some URLs", "Notice", "Search Console",
                         "These URLs may be outside the selected Search Console property, or the daily inspection quota was reached.",
                         ["Check the URLs belong to the selected property (Domain vs https:// property).", "Try again tomorrow if the quota was reached (2,000 inspections per day per property)."],
                         failed_rows, help_url=DOC["page_indexing"], source="gsc"))
    out.sort(key=lambda row: (SEVERITY_ORDER.get(row["severity"], 3), -row["count"]))
    return out
