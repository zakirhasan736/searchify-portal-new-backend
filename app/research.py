"""DataForSEO research for Keywords, Backlinks, and AI visibility.

Every result is stored per website so the same lookup is not paid for twice inside its window.
A daily call budget per customer keeps a large site from running up the bill.
Nothing is invented: an empty provider answer is returned as empty.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app import config
from app.models import FeatureRecord, User

BASE = "https://api.dataforseo.com/v3"
DAILY_CALLS = {"starter": 40, "growth": 150, "agency": 500, "scale": 800}
WINDOWS = {"keywords": timedelta(hours=24), "backlinks": timedelta(hours=24), "visibility": timedelta(days=7), "models": timedelta(days=7), "audit": timedelta(days=7)}
AUDIT_PAGES = {"starter": 100, "growth": 300, "agency": 1000, "scale": 1000}
AUDIT_STALE = timedelta(hours=3)
ENGINES = {"ChatGPT": "chat_gpt", "Gemini": "gemini", "Perplexity": "perplexity", "Claude": "claude"}
COUNTRIES = {
    "canada": ("Canada", "CA"),
    "united kingdom": ("United Kingdom", "GB"),
    "uk": ("United Kingdom", "GB"),
    "england": ("United Kingdom", "GB"),
    "australia": ("Australia", "AU"),
    "new zealand": ("New Zealand", "NZ"),
    "ireland": ("Ireland", "IE"),
    "india": ("India", "IN"),
    "bangladesh": ("Bangladesh", "BD"),
    "germany": ("Germany", "DE"),
    "united states": ("United States", "US"),
    "usa": ("United States", "US"),
}
PROVIDER_ERRORS = {
    40104: "The DataForSEO account is not verified yet. Finish verification in the DataForSEO panel.",
    40200: "The DataForSEO balance is empty. Add funds in the DataForSEO panel.",
    40210: "The DataForSEO balance is too low for this request. Add funds in the DataForSEO panel.",
    40100: "DataForSEO rejected the login. Check the API login and password in the backend settings.",
    40204: "This DataForSEO API is not active on the account. Turn it on in the DataForSEO panel.",
}


class ResearchError(Exception):
    pass


def configured() -> bool:
    return bool(config.DATAFORSEO_LOGIN and config.DATAFORSEO_PASSWORD)


def host_of(value: str) -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"^[a-z]+://", "", raw).split("/")[0].split("?")[0]
    return raw.removeprefix("www.")


def location_for(text: str = "") -> tuple[str, str]:
    low = (text or "").lower()
    for key, value in COUNTRIES.items():
        if re.search(rf"\b{re.escape(key)}\b", low):
            return value
    return ("United States", "US")


def _record(db: Session, user_id: int, kind: str, title: str) -> FeatureRecord | None:
    return (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind, FeatureRecord.title == title)
        .order_by(FeatureRecord.id.desc())
        .first()
    )


def _save(db: Session, user_id: int, kind: str, title: str, payload: dict) -> None:
    row = _record(db, user_id, kind, title)
    if row is None:
        db.add(FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload, status="stored"))
    else:
        row.payload = payload
        row.status = "stored"
    db.commit()


def cached(db: Session, user_id: int, kind: str, title: str, window: timedelta) -> dict | None:
    row = _record(db, user_id, kind, title)
    payload = dict((row.payload if row else {}) or {})
    stamp = payload.get("fetchedAt")
    if not stamp:
        return None
    try:
        fresh = datetime.utcnow() - datetime.fromisoformat(stamp) < window
    except ValueError:
        return None
    return payload if fresh else None


def _budget(db: Session, user: User, calls: int) -> None:
    today = datetime.utcnow().date().isoformat()
    row = _record(db, user.id, "dfs-usage", "DataForSEO usage")
    usage = dict((row.payload if row else {}) or {})
    if usage.get("date") != today:
        usage = {"date": today, "calls": 0, "cost": 0.0}
    plan = (getattr(user, "plan", None) or "starter").lower()
    limit = DAILY_CALLS["scale"] if user.role == "ROLE_ADMIN" else DAILY_CALLS.get(plan, DAILY_CALLS["starter"])
    if usage["calls"] + calls > limit:
        raise ResearchError(f"Today's research limit for this plan is used ({limit} lookups). It resets tomorrow.")


def _spend(db: Session, user: User, calls: int, cost: float) -> None:
    today = datetime.utcnow().date().isoformat()
    row = _record(db, user.id, "dfs-usage", "DataForSEO usage")
    usage = dict((row.payload if row else {}) or {})
    if usage.get("date") != today:
        usage = {"date": today, "calls": 0, "cost": 0.0}
    usage["calls"] = int(usage.get("calls") or 0) + calls
    usage["cost"] = round(float(usage.get("cost") or 0) + float(cost or 0), 4)
    _save(db, user.id, "dfs-usage", "DataForSEO usage", usage)


def _call(method: str, path: str, payload: list | None = None, timeout: float = 60.0) -> tuple[list, float]:
    """Returns (result list of the first task, cost). Raises ResearchError with a plain message."""
    task, cost = _task(method, path, payload, timeout)
    return list(task.get("result") or []), cost


def _task(method: str, path: str, payload: list | None = None, timeout: float = 60.0, ok: tuple[int, ...] = (20000,)) -> tuple[dict, float]:
    if not configured():
        raise ResearchError("DataForSEO is not connected. Add the API login and password to the backend settings.")
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method,
                f"{BASE}{path}",
                auth=(config.DATAFORSEO_LOGIN, config.DATAFORSEO_PASSWORD),
                json=payload,
            )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ResearchError(f"DataForSEO could not be reached. {str(exc)[:120]}") from exc
    code = int(data.get("status_code") or 0)
    if code != 20000:
        raise ResearchError(PROVIDER_ERRORS.get(code) or f"DataForSEO: {data.get('status_message') or 'request failed'}")
    task = (data.get("tasks") or [{}])[0] or {}
    task_code = int(task.get("status_code") or 0)
    if task_code and task_code not in ok:
        raise ResearchError(PROVIDER_ERRORS.get(task_code) or f"DataForSEO: {task.get('status_message') or 'task failed'}")
    return task, float(data.get("cost") or 0)


def status() -> dict:
    if not configured():
        return {"connected": False, "ready": False, "message": "Add the DataForSEO API login and password to the backend settings."}
    try:
        result, _ = _call("GET", "/appendix/user_data", None, timeout=20)
    except ResearchError as exc:
        return {"connected": True, "ready": False, "message": str(exc)}
    info = (result or [{}])[0] or {}
    balance = (info.get("money") or {}).get("balance")
    try:
        _call("POST", "/dataforseo_labs/google/keyword_overview/live", [{"keywords": ["seo"], "location_name": "United States", "language_code": "en"}], timeout=30)
    except ResearchError as exc:
        return {"connected": True, "ready": False, "balance": balance, "message": str(exc)}
    return {"connected": True, "ready": True, "balance": balance, "message": "DataForSEO is connected."}


def _kw_row(item: dict) -> dict:
    data = item.get("keyword_data") or item
    info = data.get("keyword_info") or {}
    props = data.get("keyword_properties") or {}
    intent = (data.get("search_intent_info") or {}).get("main_intent") or ""
    return {
        "keyword": data.get("keyword") or "",
        "volume": info.get("search_volume"),
        "cpc": info.get("cpc"),
        "difficulty": props.get("keyword_difficulty"),
        "intent": intent,
    }


def keywords(db: Session, user: User, *, site: str, terms: list[str], country: str = "", force: bool = False) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.")
    location, _iso = location_for(country)
    clean = [t.strip()[:80] for t in dict.fromkeys(terms or []) if t and t.strip()][:50]
    title = f"Keywords {host} {location}"
    saved = None if force else cached(db, user.id, "dfs-keywords", title, WINDOWS["keywords"])
    if saved and set(clean) <= set(saved.get("terms") or []):
        return {**saved, "cached": True}
    _budget(db, user, 3)
    cost = 0.0
    ranked_res, c = _call(
        "POST",
        "/dataforseo_labs/google/ranked_keywords/live",
        [{"target": host, "location_name": location, "language_code": "en", "limit": 100,
          "order_by": ["keyword_data.keyword_info.search_volume,desc"]}],
    )
    cost += c
    ranked = []
    for item in ((ranked_res or [{}])[0] or {}).get("items") or []:
        row = _kw_row(item)
        serp = (item.get("ranked_serp_element") or {}).get("serp_item") or {}
        changes = serp.get("rank_changes") or {}
        row.update({
            "position": serp.get("rank_group"),
            "previous": changes.get("previous_rank_absolute"),
            "isNew": bool(changes.get("is_new")),
            "url": serp.get("url") or "",
            "page": serp.get("relative_url") or "",
        })
        if row["keyword"]:
            ranked.append(row)
    by_term = {row["keyword"].lower(): row for row in ranked}

    tracked = []
    if clean:
        overview_res, c = _call(
            "POST",
            "/dataforseo_labs/google/keyword_overview/live",
            [{"keywords": clean, "location_name": location, "language_code": "en", "include_serp_info": False}],
        )
        cost += c
        found = {}
        for item in ((overview_res or [{}])[0] or {}).get("items") or []:
            row = _kw_row(item)
            found[row["keyword"].lower()] = row
        for term in clean:
            base = found.get(term.lower()) or {"keyword": term, "volume": None, "difficulty": None, "intent": ""}
            rank = by_term.get(term.lower()) or {}
            tracked.append({**base, "keyword": term, "position": rank.get("position"), "previous": rank.get("previous"), "page": rank.get("page") or ""})

    ideas = []
    seed = clean[0] if clean else (ranked[0]["keyword"] if ranked else "")
    if seed:
        ideas_res, c = _call(
            "POST",
            "/dataforseo_labs/google/keyword_suggestions/live",
            [{"keyword": seed, "location_name": location, "language_code": "en", "limit": 30,
              "order_by": ["keyword_info.search_volume,desc"]}],
        )
        cost += c
        known = {t.lower() for t in clean} | set(by_term)
        for item in ((ideas_res or [{}])[0] or {}).get("items") or []:
            row = _kw_row(item)
            if row["keyword"] and row["keyword"].lower() not in known:
                ideas.append(row)
    _spend(db, user, 3 if seed else 2, cost)
    payload = {
        "host": host,
        "location": location,
        "terms": clean,
        "ranked": ranked,
        "tracked": tracked,
        "ideas": ideas[:25],
        "source": "DataForSEO Labs",
        "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
    }
    _save(db, user.id, "dfs-keywords", title, payload)
    return {**payload, "cached": False}


def backlinks(db: Session, user: User, *, site: str, force: bool = False) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.")
    title = f"Backlinks {host}"
    saved = None if force else cached(db, user.id, "dfs-backlinks", title, WINDOWS["backlinks"])
    if saved:
        return {**saved, "cached": True}
    _budget(db, user, 2)
    cost = 0.0
    summary_res, c = _call("POST", "/backlinks/summary/live", [{"target": host, "include_subdomains": True}])
    cost += c
    s = (summary_res or [{}])[0] or {}
    links_res, c = _call(
        "POST",
        "/backlinks/backlinks/live",
        [{"target": host, "mode": "one_per_domain", "limit": 100, "include_subdomains": True, "order_by": ["rank,desc"]}],
    )
    cost += c
    links = []
    for item in ((links_res or [{}])[0] or {}).get("items") or []:
        state = "Lost" if item.get("is_lost") else "New" if item.get("is_new") else "Active"
        links.append({
            "domain": item.get("domain_from") or "",
            "source": item.get("url_from") or "",
            "target": item.get("url_to") or "",
            "anchor": item.get("anchor") or "",
            "follow": "Follow" if item.get("dofollow") else "Nofollow",
            "state": state,
            "rank": item.get("rank"),
            "seen": f"First seen {str(item.get('first_seen') or '')[:10]} · last seen {str(item.get('last_seen') or '')[:10]}",
        })
    _spend(db, user, 2, cost)
    payload = {
        "host": host,
        "summary": {
            "backlinks": s.get("backlinks"),
            "referringDomains": s.get("referring_domains"),
            "referringMainDomains": s.get("referring_main_domains"),
            "nofollowDomains": s.get("referring_domains_nofollow"),
            "brokenBacklinks": s.get("broken_backlinks"),
            "rank": s.get("rank"),
        },
        "links": links,
        "source": "DataForSEO Backlinks",
        "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
    }
    _save(db, user.id, "dfs-backlinks", title, payload)
    return {**payload, "cached": False}


def _model_for(db: Session, user: User, engine: str) -> str:
    title = f"Models {engine}"
    saved = cached(db, user.id, "dfs-models", title, WINDOWS["models"])
    if saved and saved.get("model"):
        return saved["model"]
    result, _ = _call("GET", f"/ai_optimization/{engine}/llm_responses/models", None, timeout=30)
    models = [m for m in result or [] if isinstance(m, dict) and m.get("model_name")]
    searchable = [m for m in models if m.get("web_search_supported") or m.get("web_search")] or models
    plain = [m for m in searchable if not m.get("reasoning")] or searchable
    cheap = [m for m in plain if re.search(r"mini|flash|sonar|haiku", m["model_name"], re.I)] or plain
    if not cheap:
        raise ResearchError(f"No {engine} model is available on the DataForSEO account.")
    model = cheap[0]["model_name"]
    _save(db, user.id, "dfs-models", title, {"model": model, "fetchedAt": datetime.utcnow().isoformat(timespec="seconds")})
    return model


def visibility(db: Session, user: User, *, site: str, brand: str, prompts: list[dict], country: str = "", force: bool = False) -> dict:
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.")
    _location, iso = location_for(country)
    names = {n.lower() for n in [brand, host, host.split(".")[0]] if n and len(n) > 2}
    results = []
    todo = []
    for prompt in prompts[:30]:
        text = str(prompt.get("text") or "").strip()[:480]
        engine_label = prompt.get("engine") if prompt.get("engine") in ENGINES else "ChatGPT"
        if not text:
            continue
        digest = hashlib.sha1(f"{iso}|{text.lower()}".encode()).hexdigest()[:16]
        title = f"Visibility {host} {engine_label} {digest}"[:200]
        saved = None if force else cached(db, user.id, "dfs-visibility", title, WINDOWS["visibility"])
        if saved:
            results.append({**saved, "cached": True})
        else:
            todo.append((text, engine_label, title, prompt.get("id")))
    if todo:
        _budget(db, user, len(todo))
    spent = 0.0
    for text, engine_label, title, prompt_id in todo:
        engine = ENGINES[engine_label]
        model = _model_for(db, user, engine)
        result, cost = _call(
            "POST",
            f"/ai_optimization/{engine}/llm_responses/live",
            [{"user_prompt": text, "model_name": model, "web_search": True, "web_search_country_iso_code": iso, "max_output_tokens": 800}],
            timeout=130,
        )
        spent += cost
        first = (result or [{}])[0] or {}
        answer, sources = [], []
        for item in first.get("items") or []:
            for section in item.get("sections") or []:
                if section.get("text"):
                    answer.append(section["text"])
                for note in section.get("annotations") or []:
                    if note.get("url"):
                        sources.append({"url": note["url"], "title": note.get("title") or ""})
        body = "\n".join(answer)
        low = body.lower()
        mentioned = any(name in low for name in names)
        cited = [s["url"] for s in sources if host_of(s["url"]) == host]
        row = {
            "id": prompt_id,
            "text": text,
            "engine": engine_label,
            "model": first.get("model_name") or model,
            "mention": mentioned,
            "citation": cited[0] if cited else "",
            "sources": sources[:12],
            "snippet": body[:1600],
            "fetchedAt": datetime.utcnow().isoformat(timespec="seconds"),
            "country": iso,
        }
        _save(db, user.id, "dfs-visibility", title, row)
        results.append({**row, "cached": False})
    if todo:
        _spend(db, user, len(todo), spent)
    return {"host": host, "results": results, "source": "DataForSEO AI Optimization"}


# key: (finding name, severity, fix, a title/description draft can fix it)
PAGE_CHECKS = {
    "is_5xx_code": ("Pages return a server error", "Critical", "Ask the host or developer to fix the server error. Google drops pages that keep failing.", False),
    "is_4xx_code": ("Pages return a not-found error", "Critical", "Restore the page or redirect it to the closest live page, then update the links that point to it.", False),
    "no_title": ("Pages have no title", "Critical", "Draft a title for each page and approve it before publishing.", True),
    "duplicate_title": ("Pages share the same title", "Warning", "Give each page a title about its own subject so Google can tell them apart.", True),
    "no_description": ("Pages have no meta description", "Warning", "Draft a description so Google shows your words instead of a random snippet.", True),
    "duplicate_description": ("Pages share the same description", "Warning", "Write a description for each page that matches what that page offers.", True),
    "title_too_long": ("Titles get cut off in Google", "Notice", "Shorten the title so the main promise fits before the cut.", True),
    "title_too_short": ("Titles are too short to compete", "Notice", "Add the service, the place, or the benefit to the title.", True),
    "irrelevant_title": ("Titles do not match the page content", "Warning", "Rewrite the title from what the page actually says.", True),
    "irrelevant_description": ("Descriptions do not match the page content", "Notice", "Rewrite the description from what the page actually says.", True),
    "no_h1_tag": ("Pages have no main heading (H1)", "Warning", "Add one clear H1 that names the page subject.", False),
    "duplicate_content": ("Pages repeat the same content", "Warning", "Merge the pages, or make each one about a different service or place.", False),
    "canonical_to_broken": ("Canonical tags point to broken pages", "Critical", "Point the canonical tag to the live version of the page.", False),
    "canonical_to_redirect": ("Canonical tags point to redirects", "Warning", "Point the canonical tag straight to the final URL.", False),
    "recursive_canonical": ("Canonical tags loop", "Warning", "Each canonical should point to one final live page.", False),
    "redirect_chain": ("Links go through redirect chains", "Notice", "Link straight to the final URL so visitors and Google skip the extra hops.", False),
    "is_redirect": ("Internal links point to redirects", "Notice", "Update internal links to the final URL.", False),
    "high_loading_time": ("Pages load slowly", "Warning", "Compress images and remove heavy scripts on these pages.", False),
    "large_page_size": ("Pages are very heavy", "Notice", "Reduce image and script weight on these pages.", False),
    "low_content_rate": ("Pages have very little text", "Notice", "Add useful text about the service, the place, and common questions.", False),
    "is_orphan_page": ("Pages have no internal links pointing to them", "Notice", "Link to these pages from related pages so visitors and Google can find them.", False),
    "no_image_alt": ("Images have no alt text", "Notice", "Describe each image in a few words for screen readers and image search.", False),
    "https_to_http_links": ("Secure pages link to insecure pages", "Notice", "Change the links to https.", False),
    "seo_friendly_url": ("URLs are hard to read", "Notice", "Use short words in URLs when you next restructure. Redirect the old URL if you change it.", False),
}
SEVERITY_ORDER = {"Critical": 0, "Warning": 1, "Notice": 2}


def _audit_target(site: str) -> tuple[str, str]:
    raw = (site or "").strip()
    if not re.match(r"^[a-z]+://", raw, re.I):
        raw = f"https://{raw}"
    netloc = re.sub(r"^[a-z]+://", "", raw, flags=re.I).split("/")[0].split("?")[0].lower()
    return netloc, raw


def _audit_limit(user: User) -> int:
    if user.role == "ROLE_ADMIN":
        return AUDIT_PAGES["scale"]
    plan = (getattr(user, "plan", None) or "starter").lower()
    return AUDIT_PAGES.get(plan, AUDIT_PAGES["starter"])


def _audit_report(task_id: str, summary: dict) -> dict:
    pages_res, _ = _call("POST", "/on_page/pages", [{"id": task_id, "limit": 1000}], timeout=90)
    items = [i for i in (((pages_res or [{}])[0] or {}).get("items") or []) if i.get("resource_type") == "html"]
    broken_res, _ = _call("POST", "/on_page/links", [{"id": task_id, "filters": ["is_broken", "=", True], "limit": 100}], timeout=60)
    broken = [
        {"from": link.get("link_from") or "", "to": link.get("link_to") or "", "status": link.get("page_to_status_code")}
        for link in (((broken_res or [{}])[0] or {}).get("items") or [])
    ]

    issues = []
    for key, (name, severity, fix, meta) in PAGE_CHECKS.items():
        hit = [i for i in items if (i.get("checks") or {}).get(key)]
        if not hit:
            continue
        issues.append({
            "key": key,
            "name": name,
            "severity": severity,
            "fix": fix,
            "meta": meta,
            "count": len(hit),
            "pages": [
                {
                    "url": i.get("url") or "",
                    "status": i.get("status_code"),
                    "title": ((i.get("meta") or {}).get("title") or "")[:200],
                    "description": ((i.get("meta") or {}).get("description") or "")[:300],
                }
                for i in hit[:25]
            ],
        })
    if broken:
        issues.append({
            "key": "broken_links",
            "name": "Links point to broken pages",
            "severity": "Critical",
            "fix": "Update or remove each broken link. Redirect the dead URL if other sites link to it.",
            "meta": False,
            "count": len(broken),
            "pages": [{"url": b["from"], "status": b["status"], "title": f"Links to {b['to']}", "description": ""} for b in broken[:25]],
        })

    domain = summary.get("domain_info") or {}
    checks = domain.get("checks") or {}
    site_checks = [
        ("ssl", "The site has no valid SSL certificate", "Critical", "Install or renew the certificate so every page loads on https."),
        ("sitemap", "No XML sitemap was found", "Warning", "Publish a sitemap and submit it in Search Console."),
        ("robots_txt", "No robots.txt file was found", "Notice", "Add a robots.txt that points to the sitemap."),
        ("test_https_redirect", "http does not redirect to https", "Warning", "Redirect every http address to its https version."),
    ]
    for key, name, severity, fix in site_checks:
        if key in checks and checks.get(key) is False:
            issues.append({"key": key, "name": name, "severity": severity, "fix": fix, "meta": False, "count": 1,
                           "pages": [{"url": f"https://{domain.get('name') or ''}/", "status": None, "title": "", "description": ""}]})

    issues.sort(key=lambda row: (SEVERITY_ORDER.get(row["severity"], 3), -row["count"]))
    metrics = summary.get("page_metrics") or {}
    status = summary.get("crawl_status") or {}
    return {
        "score": round(float(metrics.get("onpage_score") or 0), 1) if metrics.get("onpage_score") is not None else None,
        "pagesCrawled": status.get("pages_crawled") or len(items),
        "maxPages": status.get("max_crawl_pages"),
        "cms": domain.get("cms") or "",
        "server": domain.get("server") or "",
        "brokenLinks": metrics.get("broken_links"),
        "duplicateTitles": metrics.get("duplicate_title"),
        "nonIndexable": metrics.get("non_indexable"),
        "issues": issues,
    }


def _summary(task_id: str) -> dict | None:
    """None while the crawl is still queued."""
    task, _ = _task("GET", f"/on_page/summary/{task_id}", None, timeout=30, ok=(20000, 40601, 40602))
    if int(task.get("status_code") or 0) != 20000:
        return None
    return (task.get("result") or [{}])[0] or {}


def audit(db: Session, user: User, *, site: str, force: bool = False) -> dict:
    """Starts a DataForSEO site crawl, or reports its progress, or returns the finished report."""
    host = host_of(site)
    if not host:
        raise ResearchError("Add the website address in setup first.")
    title = f"Audit {host}"
    row = _record(db, user.id, "dfs-audit", title)
    saved = dict((row.payload if row else {}) or {})
    now = datetime.utcnow()

    def age(stamp: str | None) -> timedelta:
        try:
            return now - datetime.fromisoformat(stamp or "")
        except ValueError:
            return timedelta.max

    if saved.get("state") == "crawling" and saved.get("taskId") and age(saved.get("startedAt")) < AUDIT_STALE:
        summary = _summary(saved["taskId"])
        crawl = (summary or {}).get("crawl_status") or {}
        if summary and summary.get("crawl_progress") == "finished":
            report = _audit_report(saved["taskId"], summary)
            payload = {**saved, "state": "ready", "report": report, "fetchedAt": now.isoformat(timespec="seconds")}
            _save(db, user.id, "dfs-audit", title, payload)
            return {**payload, "cached": False}
        payload = {**saved, "crawled": crawl.get("pages_crawled") or 0, "queued": crawl.get("pages_in_queue") or 0}
        return {**payload, "cached": False}

    if saved.get("state") == "ready" and not force and age(saved.get("fetchedAt")) < WINDOWS["audit"]:
        return {**saved, "cached": True}

    _budget(db, user, 1)
    netloc, start = _audit_target(site)
    limit = _audit_limit(user)
    task, cost = _task(
        "POST",
        "/on_page/task_post",
        [{
            "target": netloc,
            "start_url": start,
            "max_crawl_pages": limit,
            "load_resources": False,
            "enable_javascript": False,
            "store_raw_html": False,
            "check_spell": False,
        }],
        ok=(20000, 20100),
    )
    task_id = task.get("id")
    if not task_id:
        raise ResearchError("DataForSEO did not start the site crawl. Try again in a minute.")
    _spend(db, user, 1, cost)
    payload = {
        "host": host,
        "state": "crawling",
        "taskId": task_id,
        "maxPages": limit,
        "startedAt": now.isoformat(timespec="seconds"),
        "crawled": 0,
        "queued": 0,
        "report": saved.get("report"),
        "fetchedAt": saved.get("fetchedAt"),
        "source": "DataForSEO On-Page",
    }
    _save(db, user.id, "dfs-audit", title, payload)
    return {**payload, "cached": False}
