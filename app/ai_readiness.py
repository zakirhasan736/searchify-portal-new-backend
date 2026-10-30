"""Why a site is (or isn't) visible in AI answers: crawler access, llms.txt, indexing,
entity signals and AI model errors.

Every reason uses the site-audit issue shape (key, name, severity, area, fix, steps,
helpUrl, count, pages, source) plus:
  * ``group``   – "llm" | "crawler" | "indexing" | "content" | "setup"
  * ``engines`` – which answer engines the reason affects (ChatGPT, Claude, Gemini …)

``run_ai_checks`` does the network work (robots.txt per AI user-agent, llms.txt, home page
robots directives and a firewall probe with AI crawler user-agents). It never raises.
The rest is pure so it is easy to test.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

from app.audit_checks import issue, page_row

AREA = "AI search"

DOC = {
    "openai_bots": "https://platform.openai.com/docs/bots",
    "anthropic_bots": "https://support.anthropic.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler",
    "perplexity_bots": "https://docs.perplexity.ai/guides/bots",
    "google_crawlers": "https://developers.google.com/search/docs/crawling-indexing/google-common-crawlers",
    "ai_features": "https://developers.google.com/search/docs/appearance/ai-features",
    "bing_bots": "https://www.bing.com/webmasters/help/which-crawlers-does-bing-use-8c184ec0",
    "apple_bots": "https://support.apple.com/en-us/119829",
    "ccbot": "https://commoncrawl.org/ccbot",
    "meta_bots": "https://developers.facebook.com/docs/sharing/webmasters/web-crawlers",
    "llms_txt": "https://llmstxt.org/",
    "snippet": "https://developers.google.com/search/docs/crawling-indexing/robots-meta-tag",
    "noindex": "https://developers.google.com/search/docs/crawling-indexing/block-indexing",
    "page_indexing": "https://support.google.com/webmasters/answer/7440203",
    "business": "https://developers.google.com/search/docs/appearance/structured-data/local-business",
    "organization": "https://developers.google.com/search/docs/appearance/structured-data/organization",
    "faq": "https://developers.google.com/search/docs/appearance/structured-data/faqpage",
    "cloudflare_ai": "https://developers.cloudflare.com/bots/concepts/bot/#ai-bots",
    "openai_keys": "https://platform.openai.com/api-keys",
    "openai_limits": "https://platform.openai.com/docs/guides/rate-limits",
}

# (user-agent, company, engines it feeds, role, help doc)
# role: "search" = fetches pages to answer / cite now; "user" = fetches when a person asks;
#       "training" = collects data to train models (blocking it does not stop citations).
AI_BOTS = [
    ("OAI-SearchBot", "OpenAI", ["ChatGPT"], "search", "openai_bots"),
    ("ChatGPT-User", "OpenAI", ["ChatGPT"], "user", "openai_bots"),
    ("GPTBot", "OpenAI", ["ChatGPT"], "training", "openai_bots"),
    ("Claude-SearchBot", "Anthropic", ["Claude"], "search", "anthropic_bots"),
    ("Claude-User", "Anthropic", ["Claude"], "user", "anthropic_bots"),
    ("ClaudeBot", "Anthropic", ["Claude"], "training", "anthropic_bots"),
    ("PerplexityBot", "Perplexity", ["Perplexity"], "search", "perplexity_bots"),
    ("Perplexity-User", "Perplexity", ["Perplexity"], "user", "perplexity_bots"),
    ("Googlebot", "Google", ["Google AI Overviews", "Gemini"], "search", "google_crawlers"),
    ("Google-Extended", "Google", ["Gemini"], "training", "google_crawlers"),
    ("Bingbot", "Microsoft", ["Copilot", "ChatGPT"], "search", "bing_bots"),
    ("Applebot", "Apple", ["Siri / Apple Intelligence"], "search", "apple_bots"),
    ("Applebot-Extended", "Apple", ["Apple Intelligence"], "training", "apple_bots"),
    ("meta-externalagent", "Meta", ["Meta AI"], "training", "meta_bots"),
    ("CCBot", "Common Crawl", ["Many AI models"], "training", "ccbot"),
]

ROLE_TEXT = {
    "search": "reads pages so the AI can answer and cite them",
    "user": "opens pages when someone asks the AI about them",
    "training": "collects pages to train future AI models",
}

# User-agent strings for the firewall probe (search bots only: the ones that matter for citations).
PROBE_AGENTS = {
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot",
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)",
}

CHALLENGE = re.compile(r"<title>\s*(just a moment|attention required|access denied|verifying you are human|ddos-guard)", re.I)


# ========================================================================================
# robots.txt rules per user-agent (Google's matching rules: most specific group, longest path)
# ========================================================================================

def _groups(text: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in (text or "").splitlines():
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
    if agents:
        groups.append((agents, rules))
    return groups


def _rules_for(groups, agent: str) -> tuple[str, list[tuple[str, str]]]:
    """(matched group name, rules) — exact product token wins over '*'; groups for one agent merge."""
    token = agent.lower()
    exact = [rules for agents, rules in groups if token in agents]
    if exact:
        return agent, [r for rules in exact for r in rules]
    star = [rules for agents, rules in groups if "*" in agents]
    if star:
        return "*", [r for rules in star for r in rules]
    return "", []


def _match(pattern: str, path: str) -> int:
    """Length of the pattern when it matches the path (robots wildcards * and $), else -1."""
    if not pattern:
        return -1
    regex = "^" + re.escape(pattern).replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return len(pattern) if re.match(regex, path) else -1


def allowed(rules: list[tuple[str, str]], path: str) -> tuple[bool, str]:
    """(allowed, deciding rule) — longest match wins; Allow wins a tie."""
    best_len, best = -1, ("allow", "")
    for kind, value in rules:
        length = _match(value, path)
        if length > best_len or (length == best_len and length >= 0 and kind == "allow"):
            best_len, best = length, (kind, value)
    if best_len < 0:
        return True, ""
    return best[0] == "allow", f"{best[0].capitalize()}: {best[1]}"


def robots_ai_access(text: str, status: int | None, paths: list[str] | None = None) -> list[dict]:
    """Per AI crawler: {agent, company, engines, role, group, allowed, blocked[{path, rule}]}."""
    paths = paths or ["/"]
    out = []
    if status is None or status >= 400:
        # No readable robots.txt: everything is allowed (Google treats 5xx as blocked; that is
        # reported by the site checks as robots_5xx).
        for agent, company, engines, role, doc in AI_BOTS:
            blocked = [{"path": "/", "rule": f"robots.txt HTTP {status}"}] if status and status >= 500 else []
            out.append({"agent": agent, "company": company, "engines": engines, "role": role, "doc": doc,
                        "group": "", "allowed": not blocked, "blocked": blocked})
        return out
    groups = _groups(text)
    for agent, company, engines, role, doc in AI_BOTS:
        group, rules = _rules_for(groups, agent)
        blocked = []
        for path in paths:
            ok, rule = allowed(rules, path)
            if not ok:
                blocked.append({"path": path, "rule": rule})
        out.append({"agent": agent, "company": company, "engines": engines, "role": role, "doc": doc,
                    "group": group, "allowed": not blocked, "blocked": blocked})
    return out


def check_llms_txt(status: int | None, text: str, content_type: str = "") -> dict:
    if status is None:
        return {"status": None, "present": False, "problem": "unreachable"}
    if status >= 400:
        return {"status": status, "present": False, "problem": "missing"}
    body = (text or "").lstrip()
    if "html" in (content_type or "").lower() or body[:15].lower().startswith(("<!doctype", "<html")):
        return {"status": status, "present": False, "problem": "html"}
    if not body.startswith("#"):
        return {"status": status, "present": True, "problem": "format", "size": len(text or "")}
    links = len(re.findall(r"\]\(https?://", body))
    return {"status": status, "present": True, "problem": "", "size": len(text or ""), "links": links,
            "title": body.splitlines()[0].lstrip("# ").strip()[:120]}


def robots_directives(meta_content: str, header: str) -> dict:
    """Snippet / AI directives from <meta name=robots> and X-Robots-Tag."""
    joined = f"{meta_content or ''},{header or ''}".lower()
    found = {d for d in ("noindex", "nosnippet", "noai", "noimageai", "none") if re.search(rf"(^|[\s,:]){d}($|[\s,])", joined)}
    if re.search(r"max-snippet\s*:\s*0\b", joined):
        found.add("max-snippet:0")
    if "none" in found:
        found |= {"noindex", "nosnippet"}
    return {"directives": sorted(found), "meta": (meta_content or "")[:200], "header": (header or "")[:200]}


_META_ROBOTS = re.compile(r"<meta[^>]+name=[\"']?(robots|googlebot)[\"']?[^>]*>", re.I)
_CONTENT = re.compile(r"content=[\"']([^\"']*)[\"']", re.I)


def _meta_robots(html: str) -> str:
    values = []
    for tag in _META_ROBOTS.finditer(html[:60000] or ""):
        m = _CONTENT.search(tag.group(0))
        if m:
            values.append(m.group(1))
    return ",".join(values)


# ========================================================================================
# Network checks
# ========================================================================================

def run_ai_checks(home: str, sample_urls: list[str] | None = None, timeout: float = 6.0) -> dict:
    """robots.txt per AI bot, llms.txt, home page directives and a firewall probe. Never raises."""
    import httpx

    from app.site_scan import AGENT, public_host

    result: dict = {"checkedAt": datetime.utcnow().isoformat(timespec="seconds"), "robots": {}, "llms": {}, "home": {}, "probe": []}
    try:
        parts = urlparse(home if "://" in home else f"https://{home}")
        host = (parts.hostname or "").lower()
        if not host or not public_host(f"https://{host}/"):
            result["error"] = "not_public"
            return result
        base = f"https://{host}"
        result["host"] = host
        paths = ["/"]
        for url in sample_urls or []:
            p = urlparse(url)
            if (p.hostname or "").lower().removeprefix("www.") == host.removeprefix("www.") and p.path and p.path not in paths:
                paths.append(p.path + (f"?{p.query}" if p.query else ""))
            if len(paths) >= 12:
                break
        with httpx.Client(headers={"User-Agent": AGENT}, timeout=timeout, follow_redirects=True) as client:
            try:
                r = client.get(f"{base}/robots.txt")
                text = r.text[:600_000] if r.status_code < 400 else ""
                result["robots"] = {"status": r.status_code, "bots": robots_ai_access(text, r.status_code, paths)}
            except httpx.HTTPError as exc:
                result["robots"] = {"status": None, "error": type(exc).__name__, "bots": robots_ai_access("", None, paths)}
            try:
                r = client.get(f"{base}/llms.txt")
                result["llms"] = check_llms_txt(r.status_code, r.text[:200_000] if r.status_code < 400 else "", r.headers.get("content-type", ""))
            except httpx.HTTPError:
                result["llms"] = check_llms_txt(None, "")
            normal_status = None
            try:
                r = client.get(f"{base}/")
                normal_status = r.status_code
                result["home"] = {"status": r.status_code, "url": str(r.url),
                                  **robots_directives(_meta_robots(r.text if r.status_code < 400 else ""), r.headers.get("x-robots-tag", ""))}
            except httpx.HTTPError as exc:
                result["home"] = {"status": None, "error": type(exc).__name__, "directives": []}

        def probe(item):
            name, ua = item
            try:
                with httpx.Client(headers={"User-Agent": ua}, timeout=timeout, follow_redirects=True) as c:
                    resp = c.get(f"{base}/")
                challenged = bool(CHALLENGE.search(resp.text[:4000])) or resp.headers.get("cf-mitigated") == "challenge"
                return {"agent": name, "status": resp.status_code, "challenge": challenged}
            except httpx.HTTPError as exc:
                return {"agent": name, "status": None, "error": type(exc).__name__}

        if normal_status and normal_status < 400:
            with ThreadPoolExecutor(max_workers=4) as pool:
                result["probe"] = list(pool.map(probe, PROBE_AGENTS.items()))
    except Exception as exc:  # noqa: BLE001 - best effort
        result["error"] = type(exc).__name__
    return result


# ========================================================================================
# Reasons
# ========================================================================================

def _reason(key, name, severity, group, engines, fix, steps, pages, *, help_url="", source="ai", technical=""):
    item = issue(key, name, severity, AREA, fix, steps, pages or [page_row("", detail=fix)], help_url=help_url, source=source)
    item["group"] = group
    item["engines"] = sorted(set(engines))
    if technical:
        item["technical"] = technical[:600]
    return item


ROBOTS_STEPS = [
    "Open yoursite/robots.txt in a browser and find the 'User-agent' group named below.",
    "Delete the 'Disallow: /' line for that crawler (or add 'Allow: /'). In WordPress: Yoast → Tools → File editor, Rank Math → General → Edit robots.txt, or your host's file manager.",
    "If you use Cloudflare, also check Security → Bots → 'Block AI bots' / AI Crawl Control is not blocking it.",
    "Save, reload robots.txt to confirm, then press Re-check here.",
]


def crawler_reasons(checks: dict) -> list[dict]:
    out = []
    bots = (checks.get("robots") or {}).get("bots") or []
    status = (checks.get("robots") or {}).get("status")
    by_role: dict[str, list[dict]] = {"search": [], "user": [], "training": []}
    for bot in bots:
        if not bot.get("allowed"):
            by_role.setdefault(bot.get("role") or "training", []).append(bot)

    def rows(items):
        return [page_row(b["blocked"][0]["path"] if b.get("blocked") else "/", title=f"{b['agent']} ({b['company']})",
                         detail=f"{b['agent']} {ROLE_TEXT.get(b['role'], '')} for {', '.join(b['engines'])}. "
                                f"Blocked by “{(b.get('blocked') or [{}])[0].get('rule', '')}”"
                                + (f" in the “User-agent: {b['group']}” group" if b.get("group") else "")
                                + (f" ({len(b['blocked'])} of the checked pages)" if len(b.get("blocked") or []) > 1 else "") + ".")
                for b in items]

    core = by_role["search"] + by_role["user"]
    google = [b for b in core if b["agent"] in {"Googlebot", "Bingbot"}]
    answer = [b for b in core if b not in google]
    if status and status >= 500:
        out.append(_reason("ai_robots_5xx", "robots.txt server error stops AI and search crawlers", "Critical", "crawler",
                           [e for b in bots for e in b["engines"]],
                           f"robots.txt returns HTTP {status}. Crawlers treat this as 'do not crawl anything'.",
                           ["Open yoursite/robots.txt — it must return 200 (or 404), never a 5xx.", "Ask your host to fix the server error.", "Re-check here."],
                           [page_row("/robots.txt", status=status)], help_url=DOC["google_crawlers"]))
        return out
    if google:
        out.append(_reason("ai_search_bots_blocked", "Google / Bing crawlers are blocked — no AI Overviews, Gemini or Copilot answers", "Critical", "crawler",
                           [e for b in google for e in b["engines"]],
                           "Google AI Overviews and Gemini use Googlebot's index; Copilot and ChatGPT search also use Bing's. Blocking them hides the site from AI answers and normal search.",
                           ROBOTS_STEPS, rows(google), help_url=DOC["ai_features"]))
    if answer:
        out.append(_reason("ai_answer_bots_blocked", "robots.txt blocks AI answer crawlers (ChatGPT, Claude, Perplexity search)", "Critical", "crawler",
                           [e for b in answer for e in b["engines"]],
                           "These crawlers fetch pages so the AI can cite and link to you. While they are blocked, these assistants can't read or cite your site.",
                           ROBOTS_STEPS, rows(answer), help_url=DOC.get(answer[0]["doc"], "")))
    if by_role["training"]:
        out.append(_reason("ai_training_bots_blocked", "AI training crawlers are blocked (your choice — check it's on purpose)", "Notice", "crawler",
                           [e for b in by_role["training"] for e in b["engines"]],
                           "Blocking training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot …) keeps your content out of future model training. "
                           "It does not stop ChatGPT search, Claude search, Perplexity or Google AI Overviews from citing you. Many sites block these on purpose.",
                           ["If this is intentional, nothing to do.", "If you want AI models to learn your brand, remove the Disallow for these user-agents in robots.txt.",
                            "Google-Extended only controls Gemini training/grounding — it does not affect Google Search or AI Overviews."],
                           rows(by_role["training"]), help_url=DOC["google_crawlers"]))
    blocked_probe = [p for p in checks.get("probe") or [] if p.get("challenge") or (p.get("status") in {401, 403, 406, 429, 451, 503})]
    if blocked_probe:
        out.append(_reason("ai_firewall_blocks", "Your firewall / CDN refuses AI crawlers", "Critical", "crawler",
                           ["ChatGPT", "Claude", "Perplexity"],
                           "The home page loads for normal visitors, but when we asked with an AI crawler's name the server refused or showed a bot challenge. "
                           "robots.txt can allow them and they are still blocked here.",
                           ["Cloudflare: Security → Bots → turn off 'Block AI bots' (or allow OAI-SearchBot, Claude-SearchBot, PerplexityBot in AI Crawl Control).",
                            "Other CDNs / security plugins (Wordfence, Sucuri, host firewall): allow these user-agents or the verified bot lists.",
                            "Real crawlers come from published IP ranges; allowing 'verified bots' is safer than allowing a user-agent string.",
                            "Re-check here."],
                           [page_row("/", status=p.get("status"), title=p["agent"],
                                     detail=f"{p['agent']} got HTTP {p.get('status')}" + (" with a bot challenge page" if p.get("challenge") else "")) for p in blocked_probe],
                           help_url=DOC["cloudflare_ai"]))
    return out


def page_signal_reasons(checks: dict) -> list[dict]:
    out = []
    home = checks.get("home") or {}
    directives = set(home.get("directives") or [])
    where = " / ".join(x for x in (f"meta robots “{home.get('meta')}”" if home.get("meta") else "", f"X-Robots-Tag “{home.get('header')}”" if home.get("header") else "") if x)
    if "noindex" in directives:
        out.append(_reason("ai_home_noindex", "The home page is set to noindex — AI search can't use it", "Critical", "indexing",
                           ["Google AI Overviews", "Gemini", "Copilot", "ChatGPT", "Perplexity"],
                           f"The home page tells crawlers not to index it ({where}). Pages that aren't indexed are not shown or cited in AI answers.",
                           ["WordPress: Settings → Reading → untick 'Discourage search engines from indexing this site'.",
                            "SEO plugin: open the page → Advanced → set 'Allow search engines to show this page' to Yes.",
                            "Check the host/CDN isn't adding an X-Robots-Tag: noindex header.", "Re-check, then request indexing in Search Console."],
                           [page_row(home.get("url") or "/", status=home.get("status"), detail=where)], help_url=DOC["noindex"]))
    snippet = directives & {"nosnippet", "max-snippet:0"}
    if snippet:
        out.append(_reason("ai_nosnippet", "nosnippet stops Google using your text in AI Overviews", "Warning", "indexing",
                           ["Google AI Overviews", "Gemini"],
                           f"The home page uses {', '.join(sorted(snippet))} ({where}). Google won't quote this page in snippets or AI Overviews.",
                           ["Remove nosnippet / max-snippet:0 from the robots meta tag (SEO plugin → Advanced → Robots meta).",
                            "To hide only part of a page use data-nosnippet on that element instead.", "Re-check here."],
                           [page_row(home.get("url") or "/", detail=where)], help_url=DOC["snippet"]))
    noai = directives & {"noai", "noimageai"}
    if noai:
        out.append(_reason("ai_noai_tag", "noai / noimageai tags are set", "Notice", "indexing", ["Many AI models"],
                           f"The home page asks AI tools not to use its content ({', '.join(sorted(noai))}). Major engines don't officially honour these, but some tools do.",
                           ["If you want AI tools to use your content, remove noai / noimageai from the robots meta tag."],
                           [page_row(home.get("url") or "/", detail=where)], help_url=DOC["snippet"]))
    if home.get("status") and home["status"] >= 400:
        out.append(_reason("ai_home_error", f"The home page returns HTTP {home['status']}", "Critical", "indexing",
                           ["Google AI Overviews", "Gemini", "Copilot", "ChatGPT", "Claude", "Perplexity"],
                           "Crawlers can't read the home page, so AI engines can't learn who you are.",
                           ["Open the home page in a private window.", "Fix the error with your host (403 is usually a firewall rule, 5xx a server problem).", "Re-check here."],
                           [page_row(home.get("url") or "/", status=home["status"])], help_url=DOC["page_indexing"]))
    llms = checks.get("llms") or {}
    problem = llms.get("problem")
    if problem in {"missing", "html", "format"}:
        name = {"missing": "No llms.txt file (optional)", "html": "llms.txt returns a web page instead of a text file",
                "format": "llms.txt isn't in the expected Markdown format"}[problem]
        fix = {
            "missing": "llms.txt is an optional, emerging standard: a short Markdown file that tells AI assistants what the site is and which pages matter. Google says it doesn't use it; some AI tools read it.",
            "html": "yoursite/llms.txt shows an HTML page (often the theme's 404 page with status 200). AI tools expecting Markdown will ignore it.",
            "format": "The file exists but doesn't start with a '# Site name' heading, so tools may not read it.",
        }[problem]
        out.append(_reason("ai_llms_txt_" + problem, name, "Notice", "setup", ["ChatGPT", "Claude", "Perplexity"], fix,
                           ["Create a plain text file named llms.txt in the site root (same place as robots.txt).",
                            "Start with '# Your business name', then a '> one-line summary', then a list of your key pages as '- [Page name](https://…): what it covers'.",
                            "Yoast and Rank Math can generate it (Yoast → Settings → llms.txt; Rank Math → General → llms.txt).",
                            "Open yoursite/llms.txt to check it shows as plain text, then Re-check."],
                           [page_row("/llms.txt", status=llms.get("status"))], help_url=DOC["llms_txt"]))
    return out


ENTITY_TYPES = re.compile(r"organization|localbusiness|corporation|store|restaurant|professionalservice|"
                          r"homeandconstructionbusiness|medicalbusiness|legalservice|financialservice|"
                          r"automotivebusiness|lodgingbusiness|foodestablishment|contractor|plumber|electrician|dentist|attorney|realestateagent", re.I)


def _page_types(page: dict) -> list[str]:
    return [str(t) for t in ((page.get("audit") or {}).get("schema") or {}).get("types") or page.get("schemaTypes") or []]


def content_reasons(scan: dict | None, profile: dict | None = None) -> list[dict]:
    pages = [p for p in (scan or {}).get("pages") or [] if not p.get("error")]
    if not pages:
        return []
    out = []
    profile = profile or {}
    home = pages[0]
    if not any(ENTITY_TYPES.search(t) for p in pages for t in _page_types(p)):
        out.append(_reason("ai_no_entity_schema", "No Organization / LocalBusiness schema — AI can't confirm who you are", "Warning", "content",
                           ["ChatGPT", "Claude", "Gemini", "Google AI Overviews", "Perplexity", "Copilot"],
                           "None of the scanned pages describes the business in structured data (name, address, phone, area served, logo, sameAs links). "
                           "Answer engines use these facts to match your brand to questions like 'best plumber in Toronto'.",
                           ["SEO plugin → Schema / Knowledge Graph: choose Organization or LocalBusiness and fill name, logo, address, phone and opening hours.",
                            "Add sameAs links to your Google Business Profile, Facebook, LinkedIn and Yelp pages.",
                            "Test the home page in Google's Rich Results Test, then scan again."],
                           [page_row(home.get("url") or "/", title=home.get("title") or "")], help_url=DOC["business"]))
    if not any(p.get("faqs") or (p.get("audit") or {}).get("questions") for p in pages):
        out.append(_reason("ai_no_questions", "No question-and-answer content for AI to quote", "Notice", "content",
                           ["ChatGPT", "Claude", "Gemini", "Google AI Overviews", "Perplexity", "Copilot"],
                           "People ask AI assistants questions. Pages that answer common questions in plain sentences (FAQ blocks, 'How much does … cost?' sections) are easier to quote.",
                           ["Add 4–6 real customer questions with short, direct answers to your main service pages.",
                            "Use a heading for each question and answer it in the first sentence.",
                            "Mark them up with FAQPage schema only if the answers are visible on the page."],
                           [page_row(p.get("url") or "", title=p.get("title") or "") for p in pages[:5]], help_url=DOC["faq"]))
    biz = str(profile.get("business") or "").strip()
    if biz and len(biz) >= 3:
        needle = re.sub(r"\s+", " ", biz.lower())
        seen = " ".join(str(home.get(k) or "") for k in ("title", "h1", "description", "text")).lower()
        if needle not in re.sub(r"\s+", " ", seen):
            out.append(_reason("ai_brand_missing_home", "Your business name isn't on the home page text", "Warning", "content",
                               ["ChatGPT", "Claude", "Gemini", "Google AI Overviews", "Perplexity", "Copilot"],
                               f"“{biz}” doesn't appear in the home page title, H1, description or main text. AI engines connect brands to pages by the name written on them.",
                               ["Put the business name, main service and city in the home page H1 or first paragraph.",
                                "Use the same name everywhere (site, Google Business Profile, social profiles).", "Scan again."],
                               [page_row(home.get("url") or "/", title=home.get("title") or "")], help_url=DOC["organization"]))
    thin = [p for p in pages if 0 < int(p.get("words") or 0) < 150 and not p.get("noindex")]
    if thin and len(thin) >= max(1, len(pages) // 3):
        out.append(_reason("ai_thin_pages", "Many pages have too little text for AI to quote", "Warning", "content",
                           ["ChatGPT", "Claude", "Gemini", "Google AI Overviews", "Perplexity", "Copilot"],
                           f"{len(thin)} of {len(pages)} scanned pages have under 150 words. AI answers quote clear, specific text — prices, areas served, process, guarantees.",
                           ["Add who it's for, what's included, price range, areas served and a short FAQ to each service page.",
                            "Write facts in sentences (not only in images or sliders).", "Scan again."],
                           [page_row(p.get("url") or "", title=p.get("title") or "", detail=f"{p.get('words')} words") for p in thin],
                           help_url="https://developers.google.com/search/docs/fundamentals/creating-helpful-content"))
    return out


def indexing_reasons(scan: dict | None, inspection: dict | None) -> list[dict]:
    out = []
    pages = [p for p in (scan or {}).get("pages") or [] if not p.get("error")]
    hidden = [p for p in pages if p.get("noindex")]
    if hidden:
        out.append(_reason("ai_pages_noindex", "Pages hidden from search and AI answers (noindex)", "Warning", "indexing",
                           ["Google AI Overviews", "Gemini", "Copilot", "ChatGPT", "Perplexity"],
                           "These pages tell search engines not to index them, so they can't appear in AI answers either. Fine for thank-you or login pages; a problem for service pages.",
                           ["Open each page in the SEO plugin → Advanced and allow indexing if the page should rank.", "Request indexing in Search Console after the change."],
                           [page_row(p.get("url") or "", title=p.get("title") or "", detail="meta robots noindex") for p in hidden], help_url=DOC["noindex"]))
    rows = (inspection or {}).get("rows") or []
    not_indexed = [r for r in rows if isinstance(r, list) and len(r) >= 3 and r[2] not in {"PASS", "—"}]
    if not_indexed:
        out.append(_reason("ai_gsc_not_indexed", "Search Console: pages not in Google's index (so not in AI Overviews)", "Warning", "indexing",
                           ["Google AI Overviews", "Gemini", "Copilot"],
                           f"Google's URL Inspection says {len(not_indexed)} of {len(rows)} checked pages are not indexed. AI Overviews and Gemini only use indexed pages.",
                           ["Open the Site audit → Search Console topic for the exact reason per page.",
                            "Fix the reason (noindex, redirect, 404, duplicate, thin content).", "Use 'Request indexing' in Search Console → URL Inspection."],
                           [page_row(r[0], detail=f"Google: {r[1]} ({r[2]})") for r in not_indexed], help_url=DOC["page_indexing"], source="gsc"))
    return out


def _llm_cause(text: str) -> tuple[str, str, list[str], str]:
    t = (text or "").lower()
    if re.search(r"rate.?limit|429|too many requests", t):
        return ("ai_llm_rate_limited", "The AI model was busy (rate limit)",
                ["Wait a minute and press Run again.", "If it keeps happening, an admin should raise the OpenAI rate limit / tier."], DOC["openai_limits"])
    if re.search(r"insufficient_quota|quota|billing|credit", t):
        return ("ai_llm_quota", "The AI model account is out of credit",
                ["An admin needs to add credit or raise the monthly limit in the OpenAI billing page.", "Then press Run again."], DOC["openai_limits"])
    if re.search(r"401|invalid.{0,10}api.?key|incorrect api key|authentication|unauthori", t):
        return ("ai_llm_key_invalid", "The AI model key is invalid or expired",
                ["An admin needs to create a new OpenAI API key and update OPENAI_API_KEY on the server.", "Then press Run again."], DOC["openai_keys"])
    if re.search(r"timeout|timed out|readtimeout|connecttimeout", t):
        return ("ai_llm_timeout", "The AI model took too long to answer",
                ["Press Run again — timeouts are usually temporary.", "If it keeps failing, email support with the technical details."], "")
    if re.search(r"model.{0,20}(not found|does not exist)|404", t):
        return ("ai_llm_model_missing", "The configured AI model doesn't exist",
                ["An admin needs to set a valid model name in the AI settings.", "Then press Run again."], "")
    return ("ai_llm_failed", "The AI model returned an error",
            ["Press Run again.", "If it keeps failing, email support with the technical details."], "")


def llm_reasons(report: dict | None, *, gsc_synced: bool = True) -> list[dict]:
    out = []
    report = report or {}
    from app.ai_visibility import ENGINES

    every = [e["name"] for e in ENGINES]
    summary = str(report.get("summary") or "")
    if report.get("status") == "unavailable" and not report.get("checks"):
        out.append(_reason("ai_no_report", "No AI visibility report yet", "Notice", "setup", every,
                           "Nothing has been estimated yet, so we can't say how AI engines see you.",
                           ["Fill in Profile (business name, services, areas).", "Press Run AI visibility."], [], source="report"))
        return out
    if "OpenAI is not configured" in summary:
        out.append(_reason("ai_llm_not_configured", "AI model not connected — this is a rules-based report", "Warning", "llm", every,
                           "The server has no AI model key, so engines were not estimated. Scores show 'Not estimated' and suggestions are generic.",
                           ["An admin needs to set OPENAI_API_KEY on the Searchify server.", "Then press Run AI visibility again."],
                           [], help_url=DOC["openai_keys"], source="report", technical=summary))
    elif summary.startswith("Visibility model failed"):
        key, name, steps, help_url = _llm_cause(summary)
        out.append(_reason(key, name, "Critical" if key in {"ai_llm_key_invalid", "ai_llm_quota", "ai_llm_model_missing"} else "Warning", "llm", every,
                           "The AI model call failed, so engines show 'Not estimated'. Your site wasn't the cause.",
                           steps, [], help_url=help_url, source="report", technical=summary))
    else:
        missing = [c for c in report.get("checks") or [] if c.get("status") == "not_checked"]
        if missing and report.get("checks"):
            engines = sorted({str(c.get("engine") or "") for c in missing if c.get("engine")})
            out.append(_reason("ai_llm_partial", f"{len(engines)} engine(s) were not estimated", "Warning", "llm", engines,
                               "The AI model didn't return a result for these engines (the reply was incomplete).",
                               ["Press Run AI visibility again.", "If the same engines keep missing, email support."], [], source="report",
                               technical=str(missing[0].get("error") or "")))
    if not gsc_synced:
        out.append(_reason("ai_gsc_not_synced", "Search Console not synced — prompts are generic", "Warning", "setup", every,
                           "Without your real Google search queries the report can only guess which questions people ask. Results are less accurate.",
                           ["Connect Google (Connections → Google) and pick the Search Console property for this site.", "Sync, then Run AI visibility again."],
                           [], help_url="https://support.google.com/webmasters/answer/34592", source="report"))
    return out


SEVERITY_ORDER = {"Critical": 0, "Warning": 1, "Notice": 2}


def ai_audit_issues(checks: dict | None, scan: dict | None = None, profile: dict | None = None) -> list[dict]:
    """AI-search findings for the site audit (no indexing duplicates — the audit shows those already)."""
    out = (crawler_reasons(checks) + page_signal_reasons(checks)) if checks else []
    out += [r for r in content_reasons(scan, profile) if r["key"] != "ai_thin_pages"]
    return sorted(out, key=lambda r: SEVERITY_ORDER.get(r["severity"], 3))


def visibility_reasons(*, report: dict | None, checks: dict | None, scan: dict | None, inspection: dict | None,
                       profile: dict | None, gsc_synced: bool = True) -> list[dict]:
    """Everything that explains the AI visibility result, worst first."""
    out = llm_reasons(report, gsc_synced=gsc_synced)
    if checks and checks.get("error"):
        out.append(_reason("ai_checks_failed", "We couldn't reach your site to check AI crawler access", "Notice", "setup", [],
                           "robots.txt, llms.txt and the firewall test didn't run, so crawler blocks are unknown (not 'OK').",
                           ["Check the website address in Setup is the public https:// address.", "Open the site in a private window to make sure it loads.",
                            "Press Re-check."], [], source="ai", technical=str(checks.get("error"))))
    elif checks:
        out += crawler_reasons(checks) + page_signal_reasons(checks)
    out += indexing_reasons(scan, inspection)
    out += content_reasons(scan, profile)
    seen, unique = set(), []
    for r in out:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        unique.append(r)
    return sorted(unique, key=lambda r: SEVERITY_ORDER.get(r["severity"], 3))


def crawler_table(checks: dict | None) -> list[dict]:
    """Compact per-bot access table for the UI."""
    return [
        {"agent": b["agent"], "company": b["company"], "engines": b["engines"], "role": b["role"], "allowed": b["allowed"],
         "rule": (b.get("blocked") or [{}])[0].get("rule", "") if b.get("blocked") else ""}
        for b in ((checks or {}).get("robots") or {}).get("bots") or []
    ]
