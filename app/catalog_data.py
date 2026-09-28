"""Relational prototype catalog for Searchify client demos.

One shared universe (site, competitors, keywords, pages, links) feeds every
screen so Home → SEO → AI → Traffic → Local → Content tell the same story.
"""

SEED_VERSION = "2026-09-24-multi-v1"

# Shared entities
SITE = "searchify.example"
SITE_URL = "https://searchify.example"
SITES = [
    ("searchify.example", "https://searchify.example", "Searchify"),
    ("demo-local.example", "https://demo-local.example", "Demo Local"),
    ("shop-north.example", "https://shop-north.example", "Shop North"),
]
COMP_A = "astra-rank.example"
COMP_B = "rivalmetrics.example"
COMP_C = "growthcheck.example"
NEWS = "news.industry"
BLOG = "seo-journal.com"
DOCS = "partner-docs.io"
FORUM = "forum.webmaster"
SPAM = "spam-links.net"
PARTNER = "partner-network.io"

KW = {
    "seo_software": "seo software",
    "site_audit": "site audit",
    "keyword_gap": "keyword gap",
    "backlink_tool": "backlink tool",
    "content_brief": "content brief",
    "rank_tracker": "rank tracker",
    "on_page": "on page seo",
    "ai_visibility": "ai visibility",
    "local_agency": "seo agency near me",
    "gbp": "gbp optimization",
    "technical": "technical seo checklist",
    "internal": "internal linking",
}

PAGES = {
    "home": "/",
    "pricing": "/pricing",
    "audit": "/blog/audit",
    "docs": "/docs",
    "features": "/features",
    "local": "/local",
}


def _s(kind, title, summary, columns, rows):
    return (kind, title, summary, columns, rows)


def expand_sites(columns, rows):
    """Duplicate rows across 3 project sites for client prototype demos."""
    if not rows:
        return list(columns), list(rows)
    if "Site" in columns or "Website" in columns:
        return list(columns), [list(r) for r in rows]
    new_cols = ["Site"] + list(columns)
    new_rows = []
    for index, (domain, _url, _label) in enumerate(SITES):
        for row in rows:
            varied = []
            for cell in row:
                text = str(cell)
                if text.isdigit() and index:
                    varied.append(str(max(1, int(text) - index * max(1, int(text) // 12))))
                elif text.endswith("%") and index:
                    try:
                        varied.append(f"{max(1, int(text[:-1]) - index)}%")
                    except ValueError:
                        varied.append(text)
                else:
                    varied.append(cell)
            new_rows.append([domain] + varied)
    return new_cols, new_rows


def seed_rows(kind: str):
    for item in SCREENS + EXTRA:
        if item[0] == kind:
            columns, rows = expand_sites(item[3], item[4])
            return {
                "summary": item[2],
                "columns": columns,
                "rows": rows,
                "panels": PANELS.get(kind),
                "seedVersion": SEED_VERSION,
            }
    return {"summary": "", "columns": [], "rows": [], "seedVersion": SEED_VERSION}


SCREENS = [
    _s(
        "get-started",
        "Get Started",
        f"Prototype project for {SITE}. Connect GSC and GA4 when providers are ready.",
        ["Step", "Status", "Detail"],
        [
            ["Add website", "Done", SITE_URL],
            ["Search Console", "Waiting for Google account", "Property not linked"],
            ["GA4", "Waiting for Google account", "Property not linked"],
            ["First crawl", "Ready", "820 pages queued"],
            ["Competitors", "Done", f"{COMP_A}, {COMP_B}"],
            ["Keyword list", "Done", "Brand + content gaps"],
        ],
    ),
    _s(
        "traffic-analytics",
        "Traffic Analytics",
        f"Sessions and engagement stored for {SITE} (prototype feed).",
        ["Metric", "Value", "WoW", "Note"],
        [
            ["Sessions", "18420", "+12%", "All channels"],
            ["Users", "12004", "+9%", "Unique"],
            ["Engagement rate", "61%", "+3%", "GA-style"],
            ["Bounce rate", "38%", "-2%", "Lower is better"],
            ["Pages / session", "2.4", "+0.1", ""],
            ["Avg. duration", "00:02:14", "+8s", ""],
            ["New users", "6840", "+11%", ""],
            ["Returning", "5164", "+6%", ""],
        ],
    ),
    _s(
        "market-overview",
        "Market Overview",
        "Category share vs tracked competitors (weekly prototype note).",
        ["Competitor", "Share", "Traffic", "Trend", "Overlap"],
        [
            [SITE, "18%", "18420", "+2%", "—"],
            [COMP_A, "27%", "31200", "+1%", "42 KW"],
            [COMP_B, "14%", "12100", "-1%", "28 KW"],
            [COMP_C, "11%", "9800", "0%", "19 KW"],
            ["Others", "30%", "—", "0%", "—"],
        ],
    ),
    _s(
        "top-pages",
        "Top Pages",
        "Landing pages joined from Search Console + analytics prototype rows.",
        ["Page", "Clicks", "Sessions", "CTR", "Position", "Status"],
        [
            [PAGES["home"], "640", "4100", "4.2%", "8.1", "Strong"],
            [PAGES["pricing"], "210", "1840", "3.1%", "11.4", "Optimize"],
            [PAGES["audit"], "186", "1510", "3.8%", "6.2", "Strong"],
            [PAGES["docs"], "120", "980", "2.9%", "14.0", "Thin"],
            [PAGES["features"], "96", "720", "2.4%", "16.2", "Gap"],
            [PAGES["local"], "74", "510", "3.0%", "9.5", "Local"],
            ["/blog/rank-tracker", "68", "440", "2.7%", "12.8", "Draft"],
            ["/compare/" + COMP_A.split(".")[0], "41", "290", "2.1%", "18.0", "New"],
        ],
    ),
    _s(
        "competitor-monitoring",
        "Competitor Monitoring",
        "Weekly traffic and keyword overlap for tracked rivals.",
        ["Domain", "Overlap", "Traffic", "Visibility", "Week", "Alert"],
        [
            [COMP_A, "42 keywords", "31200", "0.21%", "This week", "Gained 6 KW"],
            [COMP_B, "28 keywords", "12100", "0.09%", "This week", "Lost 2 KW"],
            [COMP_C, "19 keywords", "9800", "0.07%", "Last week", "Flat"],
            ["semrush.com", "8 keywords", "—", "—", "Watchlist", "Brand only"],
        ],
    ),
    _s(
        "traffic-distribution",
        "Traffic Distribution",
        "Channel mix stored from analytics prototype.",
        ["Channel", "Sessions", "Share", "WoW"],
        [
            ["Organic Search", "8473", "46%", "+4%"],
            ["Direct", "4052", "22%", "+1%"],
            ["Paid Search", "2026", "11%", "-2%"],
            ["Referral", "1658", "9%", "0%"],
            ["Organic Social", "1289", "7%", "+3%"],
            ["Email", "922", "5%", "+1%"],
        ],
    ),
    _s(
        "ai-traffic",
        "AI Traffic",
        "Sessions that arrived from AI products (prototype attribution).",
        ["Source", "Sessions", "Share", "Top landing", "Trend"],
        [
            ["ChatGPT", "186", "49%", PAGES["home"], "Up"],
            ["Perplexity", "74", "19%", PAGES["audit"], "Up"],
            ["Gemini", "41", "11%", PAGES["docs"], "Flat"],
            ["Copilot", "28", "7%", PAGES["pricing"], "New"],
            ["AI Overview", "22", "6%", PAGES["features"], "Up"],
            ["Other AI", "30", "8%", PAGES["home"], "Flat"],
        ],
    ),
    _s(
        "referral",
        "Referral",
        "Referring sites aligned with the backlink prototype index.",
        ["Source", "Medium", "Sessions", "Share", "Also links?"],
        [
            [NEWS, "referral", "96", "34%", "Yes"],
            [BLOG, "referral", "64", "23%", "Yes"],
            [DOCS, "referral", "41", "15%", "Yes"],
            [FORUM, "referral", "28", "10%", "Lost link"],
            ["producthunt.com", "referral", "22", "8%", "No"],
            ["linkedin.com", "social", "18", "6%", "No"],
        ],
    ),
    _s(
        "organic-search",
        "Organic Search",
        "Search Console–style queries for the prototype property.",
        ["Query", "Clicks", "Impressions", "CTR", "Position", "Page"],
        [
            [KW["seo_software"], "120", "3400", "3.5%", "8.2", PAGES["home"]],
            [KW["site_audit"], "86", "2100", "4.1%", "6.4", PAGES["audit"]],
            [KW["keyword_gap"], "54", "1800", "3.0%", "11.1", PAGES["docs"]],
            [KW["rank_tracker"], "41", "1600", "2.6%", "14.0", PAGES["features"]],
            [KW["content_brief"], "38", "980", "3.9%", "6.0", PAGES["pricing"]],
            [KW["backlink_tool"], "32", "1400", "2.3%", "18.2", PAGES["pricing"]],
            [KW["ai_visibility"], "28", "720", "3.9%", "9.1", PAGES["features"]],
            [KW["on_page"], "24", "1100", "2.2%", "15.4", PAGES["audit"]],
            ["searchify", "210", "4200", "5.0%", "1.2", PAGES["home"]],
            ["searchify seo", "96", "2100", "4.6%", "3.4", PAGES["home"]],
        ],
    ),
    _s(
        "ai-search",
        "AI Search",
        "Stored AI-answer snapshots for prompts about this niche.",
        ["Prompt", "Mentioned", "Linked", "Source", "URL"],
        [
            ["best seo workspace", "Yes", "Yes", "ChatGPT", PAGES["home"]],
            ["seo audit tool", "Yes", "No", "Perplexity", PAGES["audit"]],
            ["rank tracker comparison", "No", "No", "Gemini", "—"],
            ["content brief generator", "Yes", "Yes", "ChatGPT", PAGES["pricing"]],
            ["ai search visibility", "Yes", "No", "Perplexity", PAGES["features"]],
            ["local seo software", "No", "No", "Copilot", "—"],
            ["backlink audit tool", "Yes", "Yes", "ChatGPT", PAGES["docs"]],
            ["keyword gap analysis", "Yes", "No", "Gemini", PAGES["docs"]],
        ],
    ),
    _s(
        "ai-search-audit",
        "AI Search Audit",
        "Crawl findings that affect how AI products cite the site.",
        ["URL", "Finding", "Score", "Priority", "Related query"],
        [
            [PAGES["home"], "OK — brand cited", "Passed", "Low", "searchify"],
            [PAGES["pricing"], "Thin content for AI answers", "Review", "High", KW["content_brief"]],
            [PAGES["docs"], "Missing FAQ schema", "Pending", "Med", KW["keyword_gap"]],
            [PAGES["audit"], "Strong how-to structure", "Passed", "Low", KW["site_audit"]],
            [PAGES["features"], "No clear entity definition", "Review", "Med", KW["ai_visibility"]],
            [PAGES["local"], "Local NAP incomplete", "Pending", "High", KW["local_agency"]],
        ],
    ),
    _s(
        "ai-visibility-report",
        "AI Search Visibility Report",
        "Period report of mentions and links in AI answers.",
        ["Period", "Mentions", "Links", "Share of voice", "Note"],
        [
            ["This month", "28", "11", "18%", "ChatGPT led"],
            ["Last month", "19", "7", "14%", "Fewer answers"],
            ["This quarter", "64", "22", "16%", "Up vs rivals"],
            ["Prior quarter", "41", "12", "11%", "Baseline"],
        ],
    ),
    _s(
        "seo-writing",
        "SEO Writing Assistant",
        "Drafts from page briefs. Publish waits for a person.",
        ["Page", "Topic", "Status", "Score", "Brief"],
        [
            [PAGES["home"], "Homepage rewrite", "Waiting for writer", "—", "Homepage brief"],
            [PAGES["pricing"], "Pricing clarity", "In review", "72", "Pricing page"],
            [PAGES["audit"], "Audit checklist article", "Approved", "81", "SEO checklist"],
            [PAGES["docs"], "Keyword gap guide", "Draft", "58", "Gap brief"],
            [PAGES["features"], "AI visibility explainer", "Waiting for writer", "—", "AI search"],
        ],
    ),
    _s(
        "topic-research",
        "Topic Research",
        "Keyword clusters shared with Topic Finder and briefs.",
        ["Cluster", "Keywords", "Volume", "Difficulty", "Opportunity"],
        [
            ["Pricing", "pricing, plans, cost, seo software pricing", "12100", "42", "High"],
            ["Audit", "seo audit, site audit, technical seo checklist", "9900", "38", "High"],
            ["Rank tracking", "rank tracker, positions, SERP", "6400", "44", "Med"],
            ["Backlinks", "backlink tool, backlink audit, referring domains", "8100", "51", "Med"],
            ["AI search", "ai visibility, ai search optimization", "3200", "29", "High"],
            ["Local", "seo agency near me, gbp optimization", "4800", "35", "Local"],
        ],
    ),
    _s(
        "seo-content-template",
        "SEO Content Template",
        "Heading templates from topic clusters.",
        ["Topic", "Headings", "KW cluster", "Status", "Owner"],
        [
            ["SEO audit", "H1, problems, fixes, checklist", "Audit", "Ready", "Editor"],
            ["Rank tracker", "H1, features, compare, FAQ", "Rank tracking", "Draft", "Writer"],
            ["Backlink audit", "H1, toxic links, disavow, tools", "Backlinks", "Ready", "Editor"],
            ["AI visibility", "H1, platforms, tracking, tips", "AI search", "Draft", "Editor"],
            ["Local SEO", "H1, GBP, map pack, reviews", "Local", "Ready", "Local lead"],
        ],
    ),
    _s(
        "link-building",
        "Link Building",
        "Prospects aligned with referring domains. Sending stays with a person.",
        ["Domain", "Authority", "Status", "Topic", "Contact"],
        [
            [BLOG, "48", "Prospect", "SEO checklist", "editor@" + BLOG],
            [NEWS, "61", "Contacted", "Site audit", "tips@" + NEWS],
            [DOCS, "44", "Replied", "Rank tracker", "partners@" + DOCS],
            ["growth-blogs.net", "36", "Prospect", KW["on_page"], "hello@growth-blogs.net"],
            ["marketing-roundup.io", "52", "Outreach draft", KW["ai_visibility"], "pitch@marketing-roundup.io"],
            [FORUM, "29", "Paused", "Q&A thread", "mods@" + FORUM],
        ],
    ),
    _s(
        "backlinks",
        "Backlinks",
        f"Indexed backlink rows for {SITE}.",
        ["Source", "Target", "Anchor", "Type", "First seen"],
        [
            [f"{NEWS}/story/seo-tools", PAGES["home"], "searchify", "Follow", "2026-08-12"],
            [f"{BLOG}/guide/audit", PAGES["pricing"], "plans", "Follow", "2026-08-20"],
            [f"{DOCS}/partners", PAGES["docs"], "documentation", "Nofollow", "2026-07-01"],
            [f"{FORUM}/thread/123", PAGES["docs"], "docs", "Follow", "2026-06-18"],
            [f"{PARTNER}/case-study", PAGES["features"], "searchify seo", "Follow", "2026-09-02"],
            [f"{BLOG}/roundup", PAGES["audit"], "site audit", "Follow", "2026-09-10"],
            [f"{NEWS}/newsletter", PAGES["home"], "seo workspace", "Nofollow", "2026-09-14"],
            [f"{SPAM}/dir", PAGES["home"], "cheap seo", "Nofollow", "2026-05-01"],
        ],
    ),
    _s(
        "referring-domains",
        "Referring Domains",
        "Domains that link in — same set as backlinks and referral traffic.",
        ["Domain", "Links", "Authority", "New/Lost", "Traffic referral"],
        [
            [NEWS, "4", "61", "New", "96 sessions"],
            [BLOG, "3", "48", "New", "64 sessions"],
            [DOCS, "2", "44", "Flat", "41 sessions"],
            [FORUM, "1", "29", "Lost 1", "28 sessions"],
            [PARTNER, "2", "55", "New", "—"],
            ["growth-blogs.net", "1", "36", "New", "—"],
            [SPAM, "1", "8", "Review", "0"],
        ],
    ),
    _s(
        "backlink-audit",
        "Backlink Audit",
        "Flagged links waiting for a person to keep or disavow.",
        ["Source", "Flag", "Authority", "Action", "Related"],
        [
            [f"{SPAM}/dir", "Toxic", "8", "Disavow", "cheap seo"],
            [f"{FORUM}/old-thread", "Review", "29", "Open", "docs"],
            [f"{PARTNER}/case-study", "Safe", "55", "Keep", "brand"],
            [f"{NEWS}/story/seo-tools", "Safe", "61", "Keep", "searchify"],
            [f"{BLOG}/guide/audit", "Safe", "48", "Keep", "plans"],
            ["expired-links.biz/x", "Toxic", "4", "Disavow", "spam"],
        ],
    ),
    _s(
        "on-page-seo",
        "On Page SEO Checker",
        f"Crawler findings for {PAGES['pricing']} on {SITE}.",
        ["Check", "Result", "Priority", "Hint"],
        [
            ["Title", "Present", "OK", "Pricing | Searchify"],
            ["H1", "Present", "OK", "Simple SEO pricing"],
            ["Canonical", "Missing", "High", "Add self-canonical"],
            ["Meta description", "Thin", "Med", "Expand to 150 chars"],
            ["Internal links", "Present", "OK", "4 from blog"],
            ["Schema", "Missing", "Med", "Add Product/Offer"],
            ["Image alt", "2 missing", "Low", "Hero + chart"],
            ["Word count", "420", "Med", "Target 900+"],
        ],
    ),
    _s(
        "site-audit",
        "Site Audit",
        f"Site-wide crawl for {SITE}. Health score 72.",
        ["Check", "Severity", "Count", "Sample URL"],
        [
            ["Missing titles", "Error", "14", PAGES["docs"]],
            ["Missing meta descriptions", "Error", "22", PAGES["features"]],
            ["Broken links", "Error", "4", "/old-guide"],
            ["Slow pages", "Warning", "8", PAGES["pricing"]],
            ["Redirect chains", "Warning", "6", "/go/audit"],
            ["Images missing alt", "Notice", "19", PAGES["home"]],
            ["Thin content", "Notice", "11", PAGES["docs"]],
            ["Duplicate titles", "Warning", "3", PAGES["local"]],
            ["Orphan pages", "Notice", "5", "/legacy"],
        ],
    ),
    _s(
        "position-tracking",
        "Position Tracking",
        "Rank history from stored checks — same keywords as Organic Rankings.",
        ["Keyword", "Position", "Change", "URL", "Volume", "Intent"],
        [
            [KW["seo_software"], "12", "+2", PAGES["home"], "12100", "Commercial"],
            [KW["site_audit"], "8", "+1", PAGES["audit"], "6600", "Informational"],
            [KW["keyword_gap"], "19", "-3", PAGES["docs"], "2400", "Commercial"],
            [KW["backlink_tool"], "24", "0", PAGES["pricing"], "3100", "Commercial"],
            [KW["content_brief"], "6", "+4", PAGES["pricing"], "2900", "Transactional"],
            [KW["rank_tracker"], "18", "0", PAGES["features"], "3600", "Commercial"],
            [KW["ai_visibility"], "14", "+2", PAGES["features"], "1900", "Informational"],
            [KW["on_page"], "21", "-1", PAGES["audit"], "8100", "Informational"],
            ["searchify", "1", "0", PAGES["home"], "2400", "Navigational"],
            [KW["local_agency"], "3", "+1", PAGES["local"], "2900", "Local"],
        ],
    ),
    _s(
        "ai-analysis",
        "AI Analysis",
        "Stored summary of prompt runs across AI surfaces.",
        ["Run", "Mentions", "Links", "Top prompt", "Summary"],
        [
            ["Weekly", "8", "3", "best seo workspace", "Brand in 3 of 5 ChatGPT answers."],
            ["Monthly", "28", "11", "seo audit tool", "Mentions rose on ChatGPT and Perplexity."],
            ["Competitor scan", "12", "2", "rank tracker", f"{COMP_A} cited more on Gemini."],
            ["Local prompts", "4", "1", KW["local_agency"], "Map-pack adjacent answers weak."],
        ],
    ),
    _s(
        "visibility-overview",
        "Visibility Overview",
        "Search Console plus stored AI mentions in one surface map.",
        ["Surface", "Status", "Value", "WoW"],
        [
            ["Search clicks", "Stored", "301", "+8%"],
            ["Search impressions", "Stored", "8900", "+5%"],
            ["AI mentions", "Stored", "28", "+9"],
            ["AI linked answers", "Stored", "11", "+4"],
            ["Competitors tracked", "Stored", "3", "0"],
            ["Brand SOV (AI)", "Stored", "18%", "+2pp"],
        ],
    ),
    _s(
        "competitor-research",
        "Competitor Research",
        "Keywords competitors rank for — feeds Keyword Gap.",
        ["Domain", "Keyword", "Position", "Volume", "We rank?"],
        [
            [COMP_A, "seo audit tool", "3", "2400", "No (gap)"],
            [COMP_A, KW["rank_tracker"], "5", "3600", "Yes #18"],
            [COMP_B, "content optimizer", "5", "1300", "No (gap)"],
            [COMP_B, KW["backlink_tool"], "9", "3100", "Yes #24"],
            [COMP_C, KW["ai_visibility"], "7", "1900", "Yes #14"],
            [COMP_A, "serp tracker", "4", "2200", "No (gap)"],
            [COMP_B, KW["on_page"], "11", "8100", "Yes #21"],
            [COMP_C, "local seo software", "6", "1600", "No (gap)"],
        ],
    ),
    _s(
        "prompt-research",
        "Prompt Research",
        "Suggested prompts with whether an answer is already stored.",
        ["Prompt", "Answer stored", "Source", "Mention", "Action"],
        [
            ["what is searchify", "Yes", "ChatGPT", "Yes", "Track"],
            ["best seo workspace", "Yes", "Perplexity", "Yes", "Track"],
            ["seo audit tool", "No", "Gemini", "No", "Create content"],
            ["rank tracker free", "No", "ChatGPT", "No", "Brief"],
            ["ai search optimization", "Yes", "Perplexity", "Partial", "Track"],
            ["gbp optimization software", "No", "Gemini", "No", "Local page"],
            ["keyword gap analysis tool", "Yes", "ChatGPT", "Yes", "Track"],
            ["content brief generator", "Yes", "Copilot", "Yes", "Track"],
        ],
    ),
    _s(
        "prompt-tracking",
        "Prompt Tracking",
        "Same prompts on a schedule — mirrors Prompt Research winners.",
        ["Prompt", "Mention", "Link", "Trend", "Last check"],
        [
            ["best seo tool", "Yes", "Yes", "Up", "2026-09-22"],
            ["seo workspace", "Yes", "No", "Flat", "2026-09-22"],
            ["site audit checklist", "No", "No", "New", "2026-09-21"],
            ["best seo workspace", "Yes", "Yes", "Up", "2026-09-22"],
            ["content brief generator", "Yes", "Yes", "Up", "2026-09-20"],
            ["keyword gap analysis tool", "Yes", "No", "Flat", "2026-09-22"],
            ["rank tracker free", "No", "No", "Down", "2026-09-19"],
        ],
    ),
    _s(
        "content-creation",
        "Content Creation",
        "Pieces from approved briefs — same titles as My Content.",
        ["Title", "Status", "Score", "Brief", "Owner"],
        [
            ["SEO checklist", "Waiting for writer", "—", "SEO checklist", "Writer"],
            ["Audit guide", "In review", "72", "SEO audit", "Editor"],
            ["Pricing outline", "Approved", "81", "Pricing page", "Editor"],
            ["AI visibility explainer", "Waiting for writer", "—", "AI search", "Writer"],
            ["Local SEO guide", "Draft", "64", "Local SEO", "Local lead"],
            ["Keyword gap playbook", "In review", "70", "Gap brief", "Editor"],
        ],
    ),
    _s(
        "content-dashboard",
        "Content Dashboard",
        "Pipeline of briefs and drafts across the prototype library.",
        ["Title", "Status", "Owner", "Cluster", "Updated"],
        [
            ["Homepage brief", "Approved", "Editor", "Brand", "2026-09-18"],
            ["Pricing page", "In review", "Writer", "Pricing", "2026-09-20"],
            ["Local SEO guide", "Draft", "Editor", "Local", "2026-09-15"],
            ["SEO checklist", "Waiting for writer", "Writer", "Audit", "2026-09-21"],
            ["AI visibility explainer", "Draft", "Editor", "AI search", "2026-09-19"],
            ["Keyword gap playbook", "In review", "Editor", "Audit", "2026-09-22"],
            ["Rank tracker comparison", "Queued", "Writer", "Rank tracking", "2026-09-12"],
            ["Backlink audit how-to", "Approved", "Editor", "Backlinks", "2026-09-10"],
        ],
    ),
    _s(
        "ai-article",
        "AI Article Generator",
        "Articles saved from briefs. Writer status stays waiting_for_writer.",
        ["Title", "Status", "Keywords", "Tokens", "Brief"],
        [
            ["How to audit a site", "Waiting for writer", KW["site_audit"], "800", "SEO audit"],
            ["Rank tracker guide", "Draft", KW["rank_tracker"], "600", "Rank tracker"],
            ["Keyword gap analysis for startups", "In review", KW["keyword_gap"], "900", "Gap brief"],
            ["AI search visibility basics", "Waiting for writer", KW["ai_visibility"], "700", "AI search"],
            ["Backlink audit checklist", "Approved", KW["backlink_tool"], "750", "Backlinks"],
        ],
    ),
    _s(
        "content-optimizer",
        "Content Optimizer",
        "Edit drafts for live URLs. Publish waits for a person.",
        ["URL", "Status", "Score", "Target KW", "Issues"],
        [
            [PAGES["pricing"], "Needs approval", "68", KW["content_brief"], "Thin, no schema"],
            [PAGES["home"], "Approved", "84", "searchify", "OK"],
            [PAGES["docs"], "Needs approval", "61", KW["keyword_gap"], "Thin, weak H2s"],
            [PAGES["features"], "In review", "74", KW["ai_visibility"], "Meta thin"],
            [PAGES["audit"], "Approved", "88", KW["site_audit"], "OK"],
            [PAGES["local"], "Needs approval", "59", KW["local_agency"], "NAP incomplete"],
        ],
    ),
    _s(
        "content-repurposing",
        "Content Repurposing",
        "Rewrite pieces already in My Content into new formats.",
        ["Source", "Status", "Format", "Channel", "Owner"],
        [
            ["Homepage brief", "Queued", "LinkedIn", "Organic Social", "Editor"],
            ["Pricing page", "Draft", "Email", "Email", "Writer"],
            ["SEO checklist", "Queued", "Carousel", "LinkedIn", "Editor"],
            ["Audit guide", "Draft", "YouTube script", "YouTube", "Writer"],
            ["Local SEO guide", "Queued", "GBP post", "Local", "Local lead"],
            ["AI visibility explainer", "Draft", "Thread", "X", "Editor"],
        ],
    ),
    _s(
        "topic-finder",
        "Topic Finder",
        "Opportunities ranked from organic queries without strong pages.",
        ["Topic", "Reason", "Volume", "Related KW", "Suggested URL"],
        [
            ["Local SEO", "Impressions without a page", "2900", KW["local_agency"], PAGES["local"]],
            ["AI search", "Rising queries", "1900", KW["ai_visibility"], PAGES["features"]],
            ["Internal linking", "Content gap", "3600", KW["internal"], "/blog/internal-links"],
            ["Technical SEO", "High volume / thin page", "2400", KW["technical"], PAGES["audit"]],
            ["SERP features", "Competitor ranks, we don't", "1800", "serp tracker", "/blog/serp"],
            ["Disavow guide", "Backlink audit demand", "1200", "disavow file", "/blog/disavow"],
        ],
    ),
    _s(
        "seo-brief",
        "SEO Brief Generator",
        "Briefs from stored headings and keywords.",
        ["Topic", "Status", "Keywords", "Cluster", "Template"],
        [
            ["Backlink audit", "Stored", "12", "Backlinks", "Ready"],
            ["Site speed", "Draft", "8", "Audit", "Draft"],
            ["Pricing page", "Stored", "10", "Pricing", "Ready"],
            ["AI search", "Stored", "9", "AI search", "Draft"],
            ["Local SEO", "Draft", "11", "Local", "Ready"],
            ["Keyword gap", "Stored", "14", "Audit", "Ready"],
        ],
    ),
    _s(
        "my-content",
        "My Content",
        "Library of pieces. Publish waits until a person agrees.",
        ["Title", "Status", "Type", "Updated", "Related URL"],
        [
            ["Homepage brief", "Draft", "Brief", "2026-09-18", PAGES["home"]],
            ["Pricing page", "Approved", "Page", "2026-09-20", PAGES["pricing"]],
            ["Local SEO guide", "In review", "Article", "2026-09-15", PAGES["local"]],
            ["SEO checklist", "Waiting for writer", "Article", "2026-09-21", PAGES["audit"]],
            ["AI visibility explainer", "Draft", "Article", "2026-09-19", PAGES["features"]],
            ["Keyword gap playbook", "In review", "Guide", "2026-09-22", PAGES["docs"]],
            ["Backlink audit how-to", "Approved", "Guide", "2026-09-10", PAGES["docs"]],
            ["Rank tracker comparison", "Queued", "Article", "2026-09-12", PAGES["features"]],
        ],
    ),
]

EXTRA = [
    _s(
        "website-overview",
        "Website overview",
        f"Visits and geography for {SITE}.",
        ["Metric", "Value", "WoW"],
        [
            ["Sessions", "18420", "+12%"],
            ["Users", "12004", "+9%"],
            ["Engagement", "61%", "+3%"],
            ["Bounce rate", "38%", "-2%"],
            ["Avg. duration", "00:02:14", "+8s"],
            ["Pages / session", "2.4", "+0.1"],
        ],
    ),
    _s(
        "keyword-ranking",
        "Keywords",
        "Organic rank cards — same set as Position Tracking.",
        ["Keyword", "Position", "Change", "URL", "Volume"],
        [
            [KW["seo_software"], "12", "+2", PAGES["home"], "12100"],
            [KW["site_audit"], "8", "+1", PAGES["audit"], "6600"],
            [KW["keyword_gap"], "19", "-3", PAGES["docs"], "2400"],
            [KW["backlink_tool"], "24", "0", PAGES["pricing"], "3100"],
            [KW["content_brief"], "6", "+4", PAGES["pricing"], "2900"],
            [KW["rank_tracker"], "18", "0", PAGES["features"], "3600"],
            [KW["ai_visibility"], "14", "+2", PAGES["features"], "1900"],
            [KW["on_page"], "21", "-1", PAGES["audit"], "8100"],
            ["searchify", "1", "0", PAGES["home"], "2400"],
            [KW["local_agency"], "3", "+1", PAGES["local"], "2900"],
        ],
    ),
    _s(
        "keyword-analyze",
        "Keyword Analyze",
        "Groups shared with Keyword Manager lists.",
        ["Group", "Keywords", "Count", "List", "Intent mix"],
        [
            ["Brand", "searchify, seo workspace, searchify seo", "3", "Brand terms", "Nav"],
            ["Pricing", "pricing, plans, cost, seo software pricing", "4", "Content gaps", "Trans"],
            ["Local", "seo agency near me, gbp optimization", "2", "Local set", "Local"],
            ["Audit", "seo audit, site audit, technical seo checklist", "3", "Content gaps", "Info"],
            ["AI", "ai visibility, ai search optimization", "2", "AI set", "Info"],
            ["Links", "backlink tool, backlink audit, referring domains", "3", "Link set", "Comm"],
        ],
    ),
    _s(
        "website-keywords",
        "Website Keywords",
        f"Organic and paid terms for {SITE}.",
        ["Keyword", "Type", "Position", "Volume", "URL"],
        [
            ["searchify", "Organic", "1", "2400", PAGES["home"]],
            ["seo platform", "Organic", "11", "4400", PAGES["features"]],
            ["seo software pricing", "Paid", "2", "1800", PAGES["pricing"]],
            [KW["rank_tracker"], "Organic", "18", "3600", PAGES["features"]],
            [KW["site_audit"], "Organic", "8", "6600", PAGES["audit"]],
            [KW["seo_software"], "Organic", "12", "12100", PAGES["home"]],
            [KW["seo_software"], "Paid", "3", "12100", PAGES["pricing"]],
            [KW["local_agency"], "Organic", "3", "2900", PAGES["local"]],
        ],
    ),
    _s(
        "generate-keywords",
        "Generate Keywords",
        "Idea lists with stored volume — feeds Topic Finder.",
        ["Keyword", "Volume", "Difficulty", "Visits", "Parent topic"],
        [
            [KW["technical"], "2400", "34", "640", "Audit"],
            [KW["on_page"], "8100", "41", "1200", "Audit"],
            ["ai search optimization", "1300", "28", "210", "AI search"],
            [KW["internal"], "3600", "32", "480", "Content"],
            ["serp tracker", "2200", "45", "180", "Rank tracking"],
            ["disavow file", "1200", "30", "90", "Backlinks"],
            ["map pack ranking", "1600", "38", "140", "Local"],
            ["content brief template", "980", "27", "160", "Pricing"],
        ],
    ),
    _s(
        "traffic-home",
        "Research Traffic",
        "Weekly research crawl sessions for the project.",
        ["Week", "Sessions", "Users", "Pages/session", "Crawl pages"],
        [
            ["2026-09-01", "2140", "1610", "2.1", "720"],
            ["2026-09-08", "2388", "1702", "2.2", "760"],
            ["2026-09-15", "2510", "1844", "2.3", "800"],
            ["2026-09-22", "2662", "1910", "2.4", "820"],
            ["2026-08-25", "1980", "1502", "2.0", "680"],
            ["2026-08-18", "1860", "1410", "2.0", "650"],
        ],
    ),
    _s(
        "keyword-gap",
        "Keyword Gap",
        f"Terms {COMP_A}/{COMP_B} rank for and {SITE} does not.",
        ["Keyword", "Competitor", "Their position", "Volume", "Opportunity"],
        [
            ["seo audit tool", COMP_A, "3", "2400", "High"],
            [KW["rank_tracker"], COMP_A, "5", "3600", "Already #18 — improve"],
            ["content optimizer", COMP_B, "5", "1300", "High"],
            ["serp tracker", COMP_A, "4", "2200", "High"],
            ["local seo software", COMP_C, "6", "1600", "Med"],
            ["ai overview tracking", COMP_B, "8", "900", "Med"],
            ["disavow tool", COMP_A, "7", "700", "Low"],
            ["seo reporting software", COMP_C, "9", "1100", "Med"],
        ],
    ),
    _s(
        "keyword-manager",
        "Keyword Manager",
        "Lists you own and lists shared with you.",
        ["List", "Keywords", "Access", "Updated", "Used in"],
        [
            ["Brand terms", "12", "Owner", "2026-09-20", "Position Tracking"],
            ["Content gaps", "28", "Owner", "2026-09-22", "Topic Finder"],
            ["Shared research", "9", "Viewer", "2026-09-18", "Keyword Analyze"],
            ["Local set", "14", "Owner", "2026-09-15", "Map Rank Tracker"],
            ["AI set", "11", "Editor", "2026-09-19", "Prompt Tracking"],
            ["Link set", "16", "Owner", "2026-09-12", "Link Building"],
        ],
    ),
    _s(
        "organic-research",
        "Organic Traffic Insights",
        f"Domain organic keywords and pages for {SITE}.",
        ["Keyword", "Position", "Volume", "URL", "Traffic est."],
        [
            [KW["seo_software"], "12", "12100", PAGES["home"], "840"],
            ["site audit checklist", "8", "6600", PAGES["audit"], "620"],
            ["keyword gap analysis", "19", "2400", PAGES["docs"], "180"],
            [KW["backlink_tool"], "24", "3100", PAGES["pricing"], "120"],
            [KW["content_brief"], "6", "2900", PAGES["pricing"], "410"],
            [KW["rank_tracker"], "18", "3600", PAGES["features"], "210"],
            [KW["ai_visibility"], "14", "1900", PAGES["features"], "160"],
            [KW["local_agency"], "3", "2900", PAGES["local"], "380"],
        ],
    ),
    _s(
        "keyword-metrics",
        "Keyword Overview",
        "Volume and difficulty for core prototype queries.",
        ["Keyword", "Volume", "Difficulty", "CPC", "Intent"],
        [
            [KW["seo_software"], "12100", "46", "$4.20", "Commercial"],
            ["backlink audit", "4400", "38", "$3.10", "Commercial"],
            [KW["ai_visibility"], "1900", "29", "$2.40", "Informational"],
            [KW["site_audit"], "6600", "36", "$3.80", "Informational"],
            [KW["keyword_gap"], "2400", "41", "$3.50", "Commercial"],
            [KW["rank_tracker"], "3600", "44", "$3.90", "Commercial"],
            [KW["local_agency"], "2900", "35", "$5.10", "Local"],
            [KW["on_page"], "8100", "41", "$2.90", "Informational"],
        ],
    ),
    _s(
        "domain-snapshot",
        "Domain Overview",
        f"Domain-level snapshot for {SITE} — matches dashboard KPIs.",
        ["Metric", "Value", "Benchmark"],
        [
            ["Authority", "42", "Niche avg 38"],
            ["Organic keywords", "1280", "+84 MoM"],
            ["Referring domains", "86", "+6 MoM"],
            ["Organic traffic", "18420", "+12% WoW"],
            ["Paid keywords", "24", "Stable"],
            ["AI mentions", "28", "+9 MoM"],
            ["Site health", "72%", "Audit"],
            ["Top country", "United States 29%", ""],
        ],
    ),
    _s(
        "backlink-analytics",
        "Backlink Analytics",
        "New and lost backlinks with referring-domain context.",
        ["Source", "Target", "Anchor", "Change", "Authority"],
        [
            [f"{NEWS}/story/seo-tools", PAGES["home"], "searchify", "New", "61"],
            [f"{BLOG}/guide/audit", PAGES["pricing"], "plans", "New", "48"],
            [f"{FORUM}/thread/123", PAGES["docs"], "docs", "Lost", "29"],
            [f"{PARTNER}/case-study", PAGES["features"], "searchify seo", "New", "55"],
            [f"{BLOG}/roundup", PAGES["audit"], "site audit", "New", "48"],
            [f"{NEWS}/newsletter", PAGES["home"], "seo workspace", "New", "61"],
            [f"{SPAM}/dir", PAGES["home"], "cheap seo", "Review", "8"],
            [f"{DOCS}/partners", PAGES["docs"], "documentation", "Flat", "44"],
        ],
    ),
    _s(
        "brand-performance",
        "Brand Performance",
        "Brand mentions and share of voice in stored AI answers.",
        ["Metric", "Value", "vs last month"],
        [
            ["Mentions", "128", "+22"],
            ["Share of voice", "18%", "+2pp"],
            ["Linked answers", "34", "+8"],
            ["ChatGPT mentions", "64", "+14"],
            ["Perplexity mentions", "28", "+6"],
            ["Gemini mentions", "18", "+2"],
            ["Negative tone", "6%", "-1pp"],
        ],
    ),
    _s(
        "brand-perception",
        "Perception",
        "How AI answers describe the brand.",
        ["Theme", "Tone", "Share", "Example prompt"],
        [
            ["Reliable", "Positive", "42%", "best seo workspace"],
            ["Expensive", "Mixed", "18%", "searchify pricing"],
            ["Fast setup", "Positive", "27%", "seo tool easy setup"],
            ["AI-ready", "Positive", "21%", "ai search optimization"],
            ["Complex UI", "Negative", "9%", "searchify review"],
            ["Great support", "Positive", "14%", "seo software support"],
        ],
    ),
    _s(
        "brand-narrative",
        "Narrative Drivers",
        "Themes that drive brand mentions over time.",
        ["Driver", "Mentions", "Trend", "Content to push"],
        [
            ["Site audit quality", "46", "Up", "Audit guide"],
            ["AI search coverage", "28", "Up", "AI visibility explainer"],
            ["Pricing clarity", "19", "Flat", "Pricing page"],
            ["Local listings", "12", "Up", "Local SEO guide"],
            ["Backlink safety", "15", "Flat", "Backlink audit how-to"],
            ["Keyword research depth", "22", "Up", "Keyword gap playbook"],
        ],
    ),
    _s(
        "brand-questions",
        "Questions",
        "Questions that trigger brand answers — overlaps Prompt Research.",
        ["Question", "Mentioned", "Source", "Linked", "Track?"],
        [
            ["best seo workspace", "Yes", "ChatGPT", "Yes", "Yes"],
            ["seo audit tool", "Yes", "Perplexity", "No", "Yes"],
            ["rank tracker free", "No", "Gemini", "No", "Create brief"],
            ["searchify vs " + COMP_A.split(".")[0], "Yes", "ChatGPT", "Yes", "Yes"],
            ["ai visibility software", "Yes", "Perplexity", "No", "Yes"],
            ["gbp optimization tool", "No", "Gemini", "No", "Local page"],
        ],
    ),
    _s(
        "paid-search",
        "Paid Search",
        "Paid keyword and ad rows for the prototype account.",
        ["Keyword", "Clicks", "Cost", "Position", "Conv.", "Landing"],
        [
            [KW["seo_software"], "220", "$840", "2", "18", PAGES["pricing"]],
            [KW["site_audit"], "140", "$510", "1", "11", PAGES["audit"]],
            [KW["rank_tracker"], "96", "$390", "3", "7", PAGES["features"]],
            ["seo software pricing", "84", "$420", "2", "14", PAGES["pricing"]],
            [KW["backlink_tool"], "62", "$280", "4", "4", PAGES["pricing"]],
            [KW["local_agency"], "48", "$310", "2", "6", PAGES["local"]],
        ],
    ),
    _s(
        "organic-social",
        "Organic Social",
        "Social referral sessions — ties to Content Repurposing channels.",
        ["Network", "Sessions", "Share", "Top post", "From content"],
        [
            ["LinkedIn", "186", "41%", "SEO checklist carousel", "SEO checklist"],
            ["X", "94", "21%", "AI visibility thread", "AI visibility explainer"],
            ["YouTube", "72", "16%", "Audit guide cut", "Audit guide"],
            ["Facebook", "54", "12%", "Local tips", "Local SEO guide"],
            ["Instagram", "28", "6%", "Pricing graphic", "Pricing page"],
            ["Other", "18", "4%", "—", "—"],
        ],
    ),
    _s(
        "local-dashboard",
        "Local Dashboard",
        "Local visibility for the prototype business profile.",
        ["Metric", "Value", "Goal"],
        [
            ["Map pack keywords", "24", "30"],
            ["Avg. rating", "4.6", "4.7"],
            ["Reviews", "182", "200"],
            ["Listing score", "78", "90"],
            ["GBP posts / mo", "6", "8"],
            ["Q&A answered", "12/14", "100%"],
            ["Directions clicks", "420", "500"],
            ["Call clicks", "186", "220"],
        ],
    ),
    _s(
        "local-listings",
        "Listing Management",
        "Directory listings and accuracy for Searchify Local demo.",
        ["Directory", "Status", "Accuracy", "Phone match", "Hours match"],
        [
            ["Google", "Live", "98%", "Yes", "Yes"],
            ["Bing", "Live", "94%", "Yes", "Yes"],
            ["Apple", "Needs update", "81%", "No", "Yes"],
            ["Yelp", "Live", "90%", "Yes", "No"],
            ["Facebook", "Live", "88%", "Yes", "Yes"],
            ["Yellow Pages", "Needs update", "72%", "No", "No"],
        ],
    ),
    _s(
        "local-reviews",
        "Review Management",
        "Review volume and ratings by period.",
        ["Period", "Reviews", "Rating", "Replied", "Waiting AI"],
        [
            ["This month", "14", "4.7", "11", "2"],
            ["Last month", "11", "4.5", "11", "0"],
            ["Quarter", "39", "4.6", "36", "2"],
            ["Year", "182", "4.6", "170", "2"],
        ],
    ),
    _s(
        "local-gbp",
        "GBP Optimization",
        "Google Business Profile checklist for the demo location.",
        ["Check", "Status", "Detail"],
        [
            ["Categories", "Complete", "SEO agency · Software"],
            ["Photos", "Needs update", "Add 4 interior shots"],
            ["Posts", "Scheduled", "2 this week"],
            ["Q&A", "Review", "2 unanswered"],
            ["Services", "Complete", "Audit, Local, Content"],
            ["Attributes", "Needs update", "Women-led missing"],
            ["Cover photo", "Complete", "OK"],
            ["Website link", "Complete", SITE_URL + PAGES["local"]],
        ],
    ),
    _s(
        "local-automations",
        "Automations",
        "Scheduled local tasks for the demo location.",
        ["Task", "Status", "Next run", "Channel"],
        [
            ["Review request", "Active", "Tomorrow", "Email"],
            ["GBP post", "Paused", "—", "GBP"],
            ["Listing sync", "Active", "Tonight", "Directories"],
            ["Q&A digest", "Active", "Friday", "Slack"],
            ["Photo reminder", "Active", "Monthly", "Email"],
        ],
    ),
    _s(
        "local-ai-agent",
        "GBP AI Agent",
        "Suggested review replies. Sending stays with a person.",
        ["Review", "Suggestion", "Status", "Rating", "Source"],
        [
            ["Great service", "Thank the guest and invite a follow-up audit.", "Waiting", "5", "Google"],
            ["Slow reply", "Apologize and offer a scheduled call.", "Draft", "3", "Google"],
            ["Clear pricing", "Thank them and link to /pricing.", "Approved", "5", "Google"],
            ["Hard to setup", "Offer onboarding help and docs link.", "Waiting", "2", "Yelp"],
            ["Love local tools", "Highlight map rank tracker.", "Draft", "5", "Google"],
        ],
    ),
    _s(
        "local-competitors",
        "Local Competitive Analysis",
        "Nearby competitors for the demo map pack.",
        ["Business", "Rating", "Reviews", "Distance", "Map pack KW overlap"],
        [
            ["Rival Agency Co", "4.5", "210", "0.4 mi", "12"],
            ["Market SEO Spot", "4.2", "160", "0.7 mi", "8"],
            ["Local Growth Hub", "4.7", "95", "1.1 mi", "6"],
            ["City Rank Labs", "4.1", "120", "1.4 mi", "9"],
            ["Neighborhood Digitals", "4.8", "64", "1.8 mi", "4"],
        ],
    ),
    _s(
        "local-map-ranks",
        "Map Rank Tracker",
        "Map pack positions — same local keywords as Position Tracking.",
        ["Keyword", "Position", "Change", "Grid avg", "Competitor ahead"],
        [
            [KW["local_agency"], "3", "+1", "4.2", "Rival Agency Co"],
            ["local seo consultant", "7", "-2", "8.1", "Market SEO Spot"],
            [KW["gbp"], "5", "0", "5.6", "Local Growth Hub"],
            ["seo company near me", "4", "+2", "4.9", "Rival Agency Co"],
            ["google business optimization", "6", "-1", "6.4", "City Rank Labs"],
            ["local citation service", "9", "0", "10.2", "Neighborhood Digitals"],
        ],
    ),
    _s(
        "home-projects",
        "Home projects",
        "Stored project snapshots for the overview table.",
        ["Website", "URL", "Health", "Visibility", "Traffic", "Keywords", "Backlinks", "Mentions"],
        [
            [SITE, SITE_URL, "72%", "0.12%", "18420", "1280", "86", "28"],
            ["demo-local.example", "https://demo-local.example", "64%", "0.04%", "4200", "310", "24", "4"],
            ["shop-north.example", "https://shop-north.example", "68%", "0.07%", "9800", "640", "41", "9"],
            [COMP_A + " (watch)", "https://" + COMP_A, "—", "0.21%", "31200", "2100", "140", "41"],
        ],
    ),
    _s(
        "seo-dashboard",
        "SEO Dashboard",
        f"SEO overview widgets for {SITE} — relational prototype feed.",
        ["Widget", "Status", "Highlight"],
        [
            ["AI Search", "Stored", "28 mentions"],
            ["SEO Overview", "Stored", "Authority 42"],
            ["Position Tracking", "Stored", "10 tracked KW"],
            ["Site Audit", "Stored", "Health 72%"],
            ["Traffic Analytics", "Stored", "18420 sessions"],
            ["Backlinks", "Stored", "86 ref. domains"],
        ],
    ),
]


# Seed drafts so ArticleStudio pages show library rows on first open
DRAFT_SEEDS = {
    "seo-writing": [
        {
            "title": "Homepage rewrite",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "Draft from Homepage brief.",
                "type": "Article",
                "keywords": "searchify, seo workspace",
                "tone": "Clear",
                "tokens": "600",
                "draft": "Homepage rewrite.\nAudience keywords: searchify, seo workspace.\n\nOutline\n1. What Searchify is\n2. Who it helps\n3. Next step CTA\n\nA writer still produces the final text.",
            },
        },
        {
            "title": "Pricing clarity",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "Draft from Pricing page brief.",
                "type": "Article",
                "keywords": "pricing, plans, seo software pricing",
                "tone": "Clear",
                "tokens": "500",
                "draft": "Pricing clarity.\n\nOutline\n1. Plan comparison\n2. Who each plan fits\n3. FAQ",
            },
        },
    ],
    "ai-article": [
        {
            "title": "How to audit a site",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "From SEO audit brief.",
                "type": "Article",
                "keywords": KW["site_audit"],
                "tone": "Clear",
                "tokens": "800",
                "draft": "How to audit a site.\n\nOutline\n1. Crawl setup\n2. Critical errors\n3. Prioritized fixes",
            },
        },
        {
            "title": "AI search visibility basics",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "From AI search brief.",
                "type": "Article",
                "keywords": KW["ai_visibility"],
                "tone": "Clear",
                "tokens": "700",
                "draft": "AI search visibility basics.\n\nOutline\n1. What AI answers are\n2. How brands get cited\n3. Tracking prompts",
            },
        },
    ],
    "seo-brief": [
        {
            "title": "Keyword gap brief",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "Brief for gap playbook.",
                "type": "Brief",
                "keywords": KW["keyword_gap"],
                "tone": "Clear",
                "tokens": "400",
                "draft": "Keyword gap brief.\nTarget: keyword gap analysis.\nH2s: What is a gap, How to find gaps, How Searchify tracks them.",
            },
        },
    ],
    "seo-content-template": [
        {
            "title": "SEO audit template",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "Template from Audit cluster.",
                "type": "Template",
                "keywords": KW["site_audit"],
                "tone": "Clear",
                "tokens": "300",
                "draft": "SEO audit template.\nH1: How to run a site audit\nH2: Crawl\nH2: Errors\nH2: Fixes\nH2: FAQ",
            },
        },
    ],
    "content-optimizer": [
        {
            "title": "Optimize /pricing",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "Needs approval score 68.",
                "type": "Optimizer",
                "keywords": KW["content_brief"],
                "tone": "Clear",
                "tokens": "400",
                "draft": "Optimize /pricing.\nAdd Product schema.\nExpand meta.\nTarget word count 900+.",
            },
        },
    ],
    "content-repurposing": [
        {
            "title": "SEO checklist → LinkedIn",
            "status": "waiting_for_writer",
            "payload": {
                "summary": "Repurpose from My Content.",
                "type": "Repurpose",
                "keywords": "seo checklist",
                "tone": "Clear",
                "tokens": "280",
                "draft": "LinkedIn carousel outline from SEO checklist.\nSlide 1: Hook\nSlide 2-5: Steps\nSlide 6: CTA",
            },
        },
    ],
}


PANELS = {
    "website-overview": {
        "trend": [["Sep 1", 1620, 1180], ["Sep 8", 1710, 1244], ["Sep 15", 1840, 1310], ["Sep 22", 1966, 1420]],
        "trendLabels": ["Clicks", "Sessions"],
        "countries": [["United States", "32%", "5890"], ["United Kingdom", "14%", "2580"], ["Nigeria", "11%", "2026"], ["Canada", "8%", "1470"], ["Germany", "6%", "1105"], ["India", "5%", "921"]],
        "devices": [["Desktop", "58%"], ["Mobile", "37%"], ["Tablet", "5%"]],
    },
    "domain-snapshot": {
        "score": 42,
        "scoreLabel": "Authority",
        "countries": [["United States", "29%", "5340"], ["United Kingdom", "16%", "2947"], ["Nigeria", "12%", "2210"], ["Canada", "9%", "1658"]],
        "trend": [["Sep 1", 40], ["Sep 8", 41], ["Sep 15", 41], ["Sep 22", 42]],
        "trendLabels": ["Authority"],
    },
    "traffic-home": {
        "countries": [["United States", "34%", "905"], ["United Kingdom", "15%", "399"], ["Nigeria", "12%", "319"], ["Canada", "7%", "186"]],
        "devices": [["Desktop", "61%"], ["Mobile", "34%"], ["Tablet", "5%"]],
        "trend": [["Sep 1", 2140], ["Sep 8", 2388], ["Sep 15", 2510], ["Sep 22", 2662]],
        "trendLabels": ["Sessions"],
    },
    "traffic-analytics": {
        "countries": [["United States", "31%", "5710"], ["United Kingdom", "13%", "2395"], ["Nigeria", "10%", "1842"], ["Canada", "8%", "1474"]],
        "devices": [["Desktop", "54%"], ["Mobile", "41%"], ["Tablet", "5%"]],
        "trend": [["Apr", 9200], ["May", 10100], ["Jun", 11400], ["Jul", 12800], ["Aug", 14200], ["Sep", 18420]],
        "trendLabels": ["Sessions"],
    },
    "organic-search": {
        "kpis": [["Clicks", "729"], ["Impressions", "21400"], ["CTR", "3.4%"], ["Avg. position", "9.1"]],
        "trend": [["Sep 1", 140, 4200], ["Sep 8", 168, 4800], ["Sep 15", 192, 5400], ["Sep 22", 229, 7000]],
        "trendLabels": ["Clicks", "Impressions"],
        "pages": [[PAGES["home"], "640", "4.2%"], [PAGES["pricing"], "210", "3.1%"], [PAGES["audit"], "186", "3.8%"]],
    },
    "organic-research": {
        "kpis": [["Keywords", "1280"], ["Traffic est.", "2920"], ["Top 10", "48"], ["Avg. position", "14.2"]],
        "trend": [["Sep 1", 48, 1600], ["Sep 8", 61, 1800], ["Sep 15", 70, 1900], ["Sep 22", 81, 2000]],
        "trendLabels": ["Clicks", "Impressions"],
    },
    "site-audit": {
        "score": 72,
        "scoreLabel": "Site health",
        "audit": [["Errors", "40"], ["Warnings", "23"], ["Notices", "35"]],
    },
    "on-page-seo": {
        "score": 68,
        "scoreLabel": "Page score",
        "audit": [["Passed", "3"], ["Failed", "2"], ["Warnings", "3"]],
    },
    "backlink-audit": {
        "score": 64,
        "scoreLabel": "Link health",
        "audit": [["Review", "1"], ["Toxic", "2"], ["Safe", "3"]],
    },
    "ai-search-audit": {
        "score": 62,
        "scoreLabel": "AI crawl score",
        "audit": [["Passed", "2"], ["Review", "2"], ["Pending", "2"]],
    },
    "position-tracking": {
        "heatmap": [
            [KW["seo_software"], [2, 2, 3, 3, 3]],
            [KW["site_audit"], [3, 3, 4, 4, 4]],
            [KW["keyword_gap"], [1, 1, 2, 2, 1]],
            [KW["backlink_tool"], [1, 1, 1, 2, 2]],
            [KW["content_brief"], [3, 4, 4, 4, 4]],
            [KW["rank_tracker"], [2, 2, 2, 2, 2]],
            [KW["ai_visibility"], [2, 2, 3, 3, 3]],
            ["searchify", [4, 4, 4, 4, 4]],
        ],
        "buckets": ["51–100", "21–50", "11–20", "4–10", "1–3"],
        "distribution": [["Top 3", "14"], ["Top 10", "52"], ["Top 20", "110"], ["Top 100", "340"]],
        "trend": [["Sep 1", 0.08], ["Sep 8", 0.09], ["Sep 15", 0.11], ["Sep 22", 0.12]],
        "trendLabels": ["Visibility %"],
    },
    "seo-dashboard": {
        "aiKpis": [["AI Visibility", "0.12%"], ["Mentions", "28"], ["Cited pages", "11"]],
        "aiPlatforms": [["ChatGPT", "14"], ["AI Overview", "4"], ["AI Mode", "3"], ["Gemini", "4"], ["Perplexity", "3"]],
        "seoKpis": [["Authority Score", "42"], ["Organic Traffic", "18420"], ["Organic Keywords", "1280"], ["Paid Keywords", "24"], ["Ref. Domains", "86"]],
        "visibilityTrend": [["Sep 1", 0.08], ["Sep 8", 0.09], ["Sep 15", 0.11], ["Sep 22", 0.12]],
        "rankBuckets": [["Top 3", "14"], ["Top 10", "52"], ["Top 20", "110"], ["Top 100", "340"]],
        "topKeywords": [
            [KW["seo_software"], "12", "+2"],
            [KW["site_audit"], "8", "+1"],
            [KW["keyword_gap"], "19", "-3"],
            [KW["backlink_tool"], "24", "0"],
            [KW["content_brief"], "6", "+4"],
        ],
        "healthScore": 72,
        "auditCounts": [["Errors", "40"], ["Warnings", "23"], ["Notices", "35"]],
        "crawledPages": [["Crawled", "820"], ["Limit", "1000"]],
        "trafficTrend": [["Apr", 9200, 6100], ["May", 10100, 6800], ["Jun", 11400, 7400], ["Jul", 12800, 8100], ["Aug", 14200, 9000], ["Sep", 18420, 12004]],
        "trafficKpis": [["Visits", "18420"], ["Unique visitors", "12004"], ["Pages / visit", "2.4"], ["Avg. duration", "00:02:14"], ["Bounce rate", "38%"]],
        "organicRankings": [
            [KW["seo_software"], "12", "+2"],
            [KW["site_audit"], "8", "+1"],
            [KW["content_brief"], "6", "+4"],
            [KW["rank_tracker"], "18", "0"],
        ],
        "backlinkRows": [
            [f"{NEWS}/story/seo-tools", PAGES["home"], "searchify", "Follow"],
            [f"{BLOG}/guide/audit", PAGES["pricing"], "plans", "Follow"],
            [f"{DOCS}/partners", PAGES["docs"], "documentation", "Nofollow"],
        ],
    },
    "keyword-ranking": {
        "heatmap": [
            [KW["seo_software"], [2, 2, 3, 3, 3]],
            [KW["site_audit"], [3, 3, 3, 4, 4]],
            [KW["keyword_gap"], [2, 1, 1, 1, 1]],
            [KW["backlink_tool"], [1, 1, 2, 2, 2]],
            [KW["content_brief"], [4, 4, 4, 4, 4]],
        ],
        "buckets": ["51–100", "21–50", "11–20", "4–10", "1–3"],
        "distribution": [["1–3", "2"], ["4–10", "2"], ["11–20", "3"], ["21–50", "3"], ["51–100", "0"]],
    },
    "backlink-analytics": {
        "kpis": [["Authority", "42"], ["New links", "18"], ["Lost links", "4"], ["Referring domains", "86"]],
        "anchors": [["searchify", "46%"], ["plans", "22%"], ["documentation", "14%"], ["other", "18%"]],
        "trend": [["Sep 1", 78], ["Sep 8", 80], ["Sep 15", 83], ["Sep 22", 86]],
        "trendLabels": ["Ref. domains"],
    },
    "backlinks": {
        "kpis": [["Backlinks", "128"], ["New", "12"], ["Lost", "3"], ["Domains", "7"]],
        "anchors": [["searchify", "40%"], ["brand", "25%"], ["naked URL", "20%"], ["other", "15%"]],
    },
    "ai-search": {
        "heatmap": [
            ["best seo workspace", [1, 2, 3, 4, 4]],
            ["seo audit tool", [0, 1, 1, 2, 2]],
            ["rank tracker comparison", [1, 1, 2, 2, 3]],
            ["content brief generator", [1, 2, 2, 3, 3]],
        ],
        "buckets": ["None", "Mention", "Link", "Top 3", "Top 1"],
    },
    "prompt-tracking": {
        "heatmap": [
            ["best seo tool", [1, 2, 2, 3, 3]],
            ["seo workspace", [0, 1, 2, 2, 2]],
            ["best seo workspace", [1, 2, 3, 3, 4]],
            ["content brief generator", [1, 1, 2, 3, 3]],
        ],
        "buckets": ["None", "Mention", "Link", "Top 3", "Top 1"],
    },
    "brand-performance": {
        "kpis": [["Mentions", "128"], ["SOV", "18%"], ["Linked", "34"]],
        "trend": [["Jun", 72], ["Jul", 88], ["Aug", 106], ["Sep", 128]],
        "trendLabels": ["Mentions"],
    },
    "local-dashboard": {
        "score": 78,
        "scoreLabel": "Listing score",
        "kpis": [["Map KW", "24"], ["Rating", "4.6"], ["Reviews", "182"]],
    },
    "local-map-ranks": {
        "heatmap": [
            [KW["local_agency"], [3, 3, 3, 4, 4]],
            ["local seo consultant", [1, 1, 2, 2, 2]],
            [KW["gbp"], [2, 2, 3, 3, 3]],
        ],
        "buckets": ["11+", "7–10", "4–6", "2–3", "1"],
    },
    "content-dashboard": {
        "kpis": [["Approved", "2"], ["In review", "2"], ["Draft", "2"], ["Waiting", "1"]],
    },
    "market-overview": {
        "trend": [["Jun", 15], ["Jul", 16], ["Aug", 17], ["Sep", 18]],
        "trendLabels": ["Share %"],
    },
    "top-pages": {
        "kpis": [["Pages", "8"], ["Clicks", "1435"], ["Sessions", "10390"]],
    },
    "keyword-gap": {
        "kpis": [["Gaps", "8"], ["High opp.", "3"], ["Already ranking", "1"]],
    },
    "visibility-overview": {
        "kpis": [["Search clicks", "301"], ["AI mentions", "28"], ["SOV", "18%"]],
    },
}
