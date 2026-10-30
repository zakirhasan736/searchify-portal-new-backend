"""Probe each DataForSEO API family independently. Never treat account-ok as healthy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from app import config  # noqa: E402

BASE = "https://api.dataforseo.com/v3"
AUTH = (config.DATAFORSEO_LOGIN, config.DATAFORSEO_PASSWORD)


def call(method: str, path: str, payload=None, timeout: float = 60.0) -> dict:
    with httpx.Client(timeout=timeout) as client:
        response = client.request(method, f"{BASE}{path}", auth=AUTH, json=payload)
    try:
        data = response.json()
    except ValueError:
        return {
            "http": response.status_code,
            "ok": False,
            "status_code": None,
            "status_message": f"non-json body ({response.status_code})",
            "task_code": None,
            "task_message": "",
            "cost": 0,
            "sample": None,
        }
    task = (data.get("tasks") or [{}])[0] or {}
    top = int(data.get("status_code") or 0)
    task_code = int(task.get("status_code") or 0)
    ok = top == 20000 and (not task_code or task_code in (20000, 20100))
    return {
        "http": response.status_code,
        "ok": ok,
        "status_code": top,
        "status_message": data.get("status_message") or "",
        "task_code": task_code or None,
        "task_message": task.get("status_message") or "",
        "cost": float(data.get("cost") or 0),
        "sample": _sample(path, task),
    }


def _sample(path: str, task: dict):
    result = task.get("result") or []
    if not result:
        return {"result_rows": 0}
    first = result[0] if isinstance(result[0], dict) else {"value": result[0]}
    if "keyword_overview" in path or "search_volume" in path:
        row = first if "keyword" in first else (first.get("items") or [first])[0] if first.get("items") else first
        info = (row.get("keyword_info") or row) if isinstance(row, dict) else {}
        return {
            "keyword": (row.get("keyword") if isinstance(row, dict) else None) or info.get("keyword"),
            "volume": info.get("search_volume"),
            "cpc": info.get("cpc"),
            "competition": info.get("competition"),
            "intent": ((row.get("search_intent_info") or {}) if isinstance(row, dict) else {}).get("main_intent"),
        }
    if "/serp/" in path:
        items = first.get("items") or []
        organic = [i for i in items if i.get("type") == "organic"][:3]
        features = sorted({i.get("type") for i in items if i.get("type")})
        return {
            "organic": [{"rank": i.get("rank_group") or i.get("rank_absolute"), "url": i.get("url"), "title": (i.get("title") or "")[:80]} for i in organic],
            "serp_features": features,
        }
    if "/backlinks/" in path:
        return {
            "referring_domains": first.get("referring_domains"),
            "backlinks": first.get("backlinks"),
            "rank": first.get("rank"),
        }
    if "/on_page/" in path:
        return {
            "tasks": len(result) if isinstance(result, list) else 1,
            "crawl_progress": first.get("crawl_progress"),
            "id": first.get("id") or task.get("id"),
        }
    if "/business_data/" in path:
        items = first.get("items") or result
        row = items[0] if items else first
        if not isinstance(row, dict):
            return {"result_rows": len(result)}
        return {
            "title": row.get("title") or row.get("name"),
            "address": row.get("address") or (row.get("address_info") or {}).get("address"),
            "category": row.get("category") or row.get("category_ids"),
        }
    if "/ai_optimization/" in path:
        if "models" in path:
            names = [m.get("model_name") or m.get("name") for m in result if isinstance(m, dict)]
            return {"models": [n for n in names if n][:8], "count": len(result)}
        return {"result_rows": len(result)}
    if "user_data" in path:
        money = (first.get("money") or {})
        return {"login": first.get("login"), "balance": money.get("balance")}
    return {"result_rows": len(result)}


PROBES = [
    {
        "id": "account",
        "label": "Account (user_data only — not product health)",
        "wired": True,
        "method": "GET",
        "path": "/appendix/user_data",
        "payload": None,
    },
    {
        "id": "keyword_data",
        "label": "Keyword Data (Google Ads search volume)",
        "wired": False,  # product uses Labs; this is the Keywords Data API product
        "method": "POST",
        "path": "/keywords_data/google_ads/search_volume/live",
        "payload": [{"keywords": ["seo tools"], "location_code": 2840, "language_code": "en"}],
    },
    {
        "id": "keyword_labs",
        "label": "Keyword Labs (app path: overview + intent)",
        "wired": True,
        "method": "POST",
        "path": "/dataforseo_labs/google/keyword_overview/live",
        "payload": [{"keywords": ["seo tools"], "location_code": 2840, "language_code": "en"}],
    },
    {
        "id": "serp",
        "label": "SERP API (organic live)",
        "wired": True,
        "method": "POST",
        "path": "/serp/google/organic/live/regular",
        "payload": [{"keyword": "seo tools", "location_code": 2840, "language_code": "en", "depth": 10}],
        "timeout": 90.0,
    },
    {
        "id": "backlinks",
        "label": "Backlinks API (summary)",
        "wired": True,
        "method": "POST",
        "path": "/backlinks/summary/live",
        "payload": [{"target": "dataforseo.com", "include_subdomains": True, "backlinks_status_type": "live"}],
    },
    {
        "id": "on_page",
        "label": "On-Page API (id_list / access)",
        "wired": True,
        "method": "POST",
        "path": "/on_page/id_list",
        "payload": [{"limit": 1}],
    },
    {
        "id": "business_data",
        "label": "Business Data API (listings search)",
        "wired": False,
        "method": "POST",
        "path": "/business_data/business_listings/search/live",
        "payload": [{
            "categories": ["software_company"],
            "location_coordinate": "40.7128,-74.0060,10",
            "limit": 1,
        }],
        "timeout": 90.0,
    },
    {
        "id": "ai_optimization",
        "label": "AI Optimization (ChatGPT models list)",
        "wired": True,
        "method": "GET",
        "path": "/ai_optimization/chat_gpt/llm_responses/models",
        "payload": None,
        "timeout": 45.0,
    },
]


def main() -> int:
    if not (config.DATAFORSEO_LOGIN and config.DATAFORSEO_PASSWORD):
        print(json.dumps({"error": "DATAFORSEO credentials missing"}, indent=2))
        return 2

    rows = []
    for probe in PROBES:
        try:
            result = call(
                probe["method"],
                probe["path"],
                probe.get("payload"),
                timeout=float(probe.get("timeout") or 60),
            )
        except Exception as exc:  # noqa: BLE001
            result = {
                "http": None,
                "ok": False,
                "status_code": None,
                "status_message": type(exc).__name__,
                "task_code": None,
                "task_message": str(exc)[:240],
                "cost": 0,
                "sample": None,
            }
        rows.append({
            "id": probe["id"],
            "label": probe["label"],
            "wired_in_app": probe["wired"],
            "path": probe["path"],
            **result,
        })

    wired = [r for r in rows if r["wired_in_app"] and r["id"] != "account"]
    healthy_wired = [r["id"] for r in wired if r["ok"]]
    failed_wired = [r["id"] for r in wired if not r["ok"]]
    report = {
        "credentials": "set",
        "account_only_ready_would_be": next((r["ok"] for r in rows if r["id"] == "account"), False),
        "wired_apis_ok": healthy_wired,
        "wired_apis_failed": failed_wired,
        "overall_ready_if_all_wired_ok": not failed_wired and bool(wired),
        "probes": rows,
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["overall_ready_if_all_wired_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
