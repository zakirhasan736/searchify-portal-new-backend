"""Source-of-truth evidence for one page. Unknown stays unknown."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


def normalize_page_url(url: str) -> str:
    """Join key for the page. Drops query, fragment, www, and a trailing slash."""
    raw = " ".join(str(url or "").split())
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def urls_match(left: str, right: str) -> bool:
    return bool(left and right) and normalize_page_url(left) == normalize_page_url(right)


def _unknown_metric(value) -> bool:
    return value in (None, "", "—", "-", "unknown")


def _stamp(value: str | None) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def _age_days(stamp: str | None) -> float | None:
    text = _stamp(stamp)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400


def _gsc_queries(raw) -> list[dict]:
    rows = []
    for item in raw or []:
        if isinstance(item, str):
            query = " ".join(item.split())
            if query:
                rows.append({"query": query, "clicks": None, "impressions": None, "ctr": None, "position": None})
            continue
        if isinstance(item, (list, tuple)) and item:
            rows.append({
                "query": " ".join(str(item[0]).split()),
                "clicks": item[1] if len(item) > 1 else None,
                "impressions": item[2] if len(item) > 2 else None,
                "ctr": item[3] if len(item) > 3 else None,
                "position": item[4] if len(item) > 4 else None,
            })
            continue
        if isinstance(item, dict) and item.get("query"):
            rows.append({
                "query": " ".join(str(item.get("query")).split()),
                "clicks": item.get("clicks"),
                "impressions": item.get("impressions"),
                "ctr": item.get("ctr"),
                "position": item.get("position"),
            })
    return [row for row in rows if row.get("query")]


def search_console_block(
    gsc: dict | None,
    *,
    page_url: str,
    property_url: str = "",
) -> dict:
    """Normalize Search Console evidence. An empty query list is not zero demand."""
    data = dict(gsc or {})
    explicit = str(data.get("status") or "").strip().lower()
    prop = _stamp(data.get("property") or property_url)
    date_range = data.get("date_range") or data.get("dateRange")
    fetched = _stamp(data.get("fetched_at") or data.get("fetchedAt") or data.get("last_successful_fetch"))
    queries = _gsc_queries(data.get("queries"))
    page_scope = str(data.get("queryScope") or data.get("query_scope") or "") == "page"
    metrics = data.get("page_metrics") if isinstance(data.get("page_metrics"), dict) else None
    if metrics is None and not any(_unknown_metric(data.get(key)) for key in ("clicks", "impressions")):
        if data.get("clicks") not in (None, "") or data.get("impressions") not in (None, ""):
            metrics = {
                "clicks": None if _unknown_metric(data.get("clicks")) else data.get("clicks"),
                "impressions": None if _unknown_metric(data.get("impressions")) else data.get("impressions"),
                "ctr": None if _unknown_metric(data.get("ctr")) else data.get("ctr"),
                "position": None if _unknown_metric(data.get("position")) else data.get("position"),
            }
            if all(value is None for value in metrics.values()):
                metrics = None
    matched = _stamp(data.get("matched_url") or data.get("matchedUrl") or "")
    warnings = [str(item) for item in (data.get("warnings") or []) if str(item).strip()]
    error = _stamp(data.get("error"))
    status = explicit or "not_verified"
    if error and status not in {"error", "mismatched", "stale"}:
        status = "error"
    if status == "not_verified" and (metrics or (queries and page_scope)):
        status = "partial" if not queries else "verified"
    if matched and page_url and not urls_match(matched, page_url):
        status = "mismatched"
        warnings.append("Search Console rows were returned for a different URL than this page.")
    age = _age_days(fetched)
    if status in {"verified", "partial"} and age is not None and age > 21:
        status = "stale"
        warnings.append("Search Console evidence is older than 21 days.")
    if not prop and status == "not_verified":
        warnings.append("No Search Console property is connected for this page.")
    if status in {"partial", "not_verified", "error"}:
        warnings.append(
            "An empty Search Console query list is not proof that this page has no relevant searches. "
            "The API can omit lower-priority rows, and performance data is typically delayed."
        )
    if queries and not page_scope and status != "mismatched":
        warnings.append("These queries are not confirmed for this exact URL. Do not treat them as page queries.")
        if status == "verified":
            status = "partial"
    if not page_scope:
        queries_for_page: list[dict] = []
    else:
        queries_for_page = queries
    return {
        "status": status,
        "property": prop,
        "page_url": page_url or None,
        "matched_url": matched or None,
        "date_range": date_range,
        "last_successful_fetch": fetched,
        "page_metrics": metrics,
        "queries": queries_for_page,
        "query_scope": "page" if page_scope else "not_page",
        "row_limit": data.get("row_limit") or data.get("rowLimit"),
        "segments": data.get("segments") or {
            "country": {"status": "not_requested"},
            "device": {"status": "not_requested"},
        },
        "error": error,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _analytics_block(analytics: dict | None, *, page_url: str) -> dict:
    data = analytics or {}
    landing = data.get("landing")
    connected = bool(data.get("connected"))
    matched = isinstance(landing, (list, tuple)) and bool(landing)
    status = "not_verified"
    if data.get("status"):
        status = str(data.get("status"))
    elif data.get("error"):
        status = "error"
    elif matched:
        status = "verified"
    elif connected:
        status = "partial"
    warning = None
    if status != "verified":
        warning = "Analytics landing-page metrics for this URL are not verified. Do not invent sessions or engagement."
    return {
        "status": status,
        "page_url": page_url or None,
        "date_range": data.get("date_range") or data.get("dateRange"),
        "landing_page_metrics": list(landing) if matched else None,
        "error": _stamp(data.get("error")),
        "warning": warning,
    }


def _dataforseo_block(research: dict | None, *, page_url: str, keyword: str = "") -> dict:
    body = research or {}
    keywords = body.get("keywords") if isinstance(body.get("keywords"), dict) else {}
    ranked = keywords.get("ranked") if isinstance(keywords.get("ranked"), list) else []
    page_rows = []
    for row in ranked:
        if not isinstance(row, dict):
            continue
        term = " ".join(str(row.get("keyword") or "").split())
        if not term:
            continue
        row_url = str(row.get("url") or "")
        if row_url and page_url and not urls_match(row_url, page_url):
            continue
        if not row_url:
            continue
        page_rows.append({
            "keyword": term,
            "volume": row.get("volume"),
            "position": row.get("position"),
            "url": row_url,
        })
    competitors = body.get("competitors") if isinstance(body.get("competitors"), list) else []
    location = keywords.get("location") or body.get("location")
    language = keywords.get("language") or body.get("language")
    if page_rows:
        status = "verified"
        warning = None
    elif ranked:
        status = "partial"
        warning = (
            "DataForSEO returned keywords, but none are tied to this page URL. "
            "Do not reuse another page's keywords as this page's demand."
        )
    else:
        status = "not_verified"
        warning = "DataForSEO keyword evidence for this page is not verified. Do not invent volume or difficulty."
    return {
        "status": status,
        "location": location,
        "language": language,
        "page_keywords": page_rows[:8],
        "competitor_results": competitors[:6] if isinstance(competitors, list) else [],
        "selected_keyword": keyword or None,
        "warning": warning,
    }


def _technical_block(page: dict | None, analytics: dict | None) -> dict:
    scraped = page or {}
    speed = (analytics or {}).get("speed") if isinstance((analytics or {}).get("speed"), dict) else {}
    readable = bool(scraped.get("url") and not scraped.get("error"))
    indexability = "noindex" if scraped.get("noindex") else ("readable" if readable else None)
    performance = None
    if speed and any(speed.get(key) for key in ("speedIndex", "lcp", "performance", "seo")):
        performance = {
            "speedIndex": speed.get("speedIndex"),
            "lcp": speed.get("lcp"),
            "performance": speed.get("performance"),
            "seo": speed.get("seo"),
            "source": speed.get("source") or "stored",
        }
    status = "verified" if readable else "not_verified"
    return {
        "status": status,
        "indexability": indexability,
        "canonical": scraped.get("canonical") or None,
        "performance": performance,
        "warning": None if readable else "The live page could not be read, so current metadata is unverified.",
    }


def build_evidence_record(
    *,
    page_url: str,
    canonical_url: str | None = None,
    current_title: str = "",
    current_description: str = "",
    og_title: str = "",
    h1: str = "",
    gsc: dict | None = None,
    analytics: dict | None = None,
    research: dict | None = None,
    page: dict | None = None,
    property_url: str = "",
    keyword: str = "",
    generated_at: str | None = None,
) -> dict:
    """One recommendation's evidence. null means unknown."""
    url = " ".join(str(page_url or "").split())
    search = search_console_block(gsc, page_url=url, property_url=property_url)
    analytics_block = _analytics_block(analytics, page_url=url)
    dataforseo = _dataforseo_block(research, page_url=url, keyword=keyword)
    technical = _technical_block(page, analytics)
    warnings = [*search["warnings"]]
    for block in (analytics_block, dataforseo, technical):
        if block.get("warning"):
            warnings.append(block["warning"])
    refs = []
    if technical["status"] == "verified":
        refs.append({"id": "page:live", "source": "live_html", "summary": "Live title, description, headings, and body were read."})
    if h1:
        refs.append({"id": "page:h1", "source": "live_html", "summary": h1[:160]})
    if search["status"] in {"verified", "partial"} and search.get("page_metrics"):
        refs.append({"id": "gsc:page", "source": "search_console", "summary": "Page totals for the recorded date range."})
    for index, row in enumerate(search.get("queries") or []):
        refs.append({"id": f"gsc:query:{index + 1}", "source": "search_console", "summary": row.get("query") or ""})
    for index, row in enumerate(dataforseo.get("page_keywords") or []):
        refs.append({"id": f"dfs:keyword:{index + 1}", "source": "dataforseo", "summary": row.get("keyword") or ""})
    return {
        "page_url": url,
        "canonical_url": canonical_url or None,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "current_metadata": {
            "title": current_title or None,
            "description": current_description or None,
            "og_title": og_title or None,
            "h1": h1 or None,
        },
        "search_console": search,
        "analytics": analytics_block,
        "dataforseo": dataforseo,
        "technical_audit": technical,
        "data_quality_warnings": list(dict.fromkeys(warnings)),
        "evidence_refs": refs,
    }


def evidence_prompt_block(record: dict) -> str:
    """Text the generator may see. It states gaps instead of filling them."""
    search = record.get("search_console") or {}
    dfs = record.get("dataforseo") or {}
    analytics = record.get("analytics") or {}
    technical = record.get("technical_audit") or {}
    lines = [
        "EVIDENCE RECORD (cite these ids only; do not invent metrics):",
        f"- page_url: {record.get('page_url') or '(missing)'}",
        f"- canonical_url: {record.get('canonical_url') or '(unknown)'}",
        f"- search_console.property: {search.get('property') or '(unknown)'}",
        f"- search_console.status: {search.get('status')}",
        f"- search_console.date_range: {search.get('date_range') or '(unknown)'}",
        f"- search_console.last_successful_fetch: {search.get('last_successful_fetch') or '(unknown)'}",
        f"- search_console.page_metrics: {search.get('page_metrics') or '(unknown)'}",
    ]
    queries = search.get("queries") or []
    if queries:
        lines.append("- search_console.page_queries:")
        for index, row in enumerate(queries[:12]):
            lines.append(
                f"  - gsc:query:{index + 1} {row.get('query')} "
                f"(clicks={row.get('clicks')}, impressions={row.get('impressions')}, "
                f"ctr={row.get('ctr')}, position={row.get('position')})"
            )
    else:
        lines.append("- search_console.page_queries: unknown. Do not treat this as an empty market.")
    lines.append(f"- analytics.status: {analytics.get('status')}")
    lines.append(f"- analytics.landing_page_metrics: {analytics.get('landing_page_metrics') or '(unknown)'}")
    lines.append(f"- dataforseo.status: {dfs.get('status')} location={dfs.get('location') or '(unknown)'} language={dfs.get('language') or '(unknown)'}")
    keywords = dfs.get("page_keywords") or []
    if keywords:
        lines.append("- dataforseo.page_keywords:")
        for index, row in enumerate(keywords[:8]):
            lines.append(
                f"  - dfs:keyword:{index + 1} {row.get('keyword')} "
                f"(volume={row.get('volume')}, position={row.get('position')})"
            )
    else:
        lines.append("- dataforseo.page_keywords: unknown. Do not invent search volume.")
    lines.append(f"- technical_audit.status: {technical.get('status')} indexability={technical.get('indexability') or '(unknown)'}")
    if technical.get("performance"):
        lines.append(
            "- technical_audit.performance is context only. A speed or SEO score does not justify a title change: "
            f"{technical.get('performance')}"
        )
    for warning in record.get("data_quality_warnings") or []:
        lines.append(f"- warning: {warning}")
    return "\n".join(lines)
