"""AI visibility report across popular answer engines.

v1 does not crawl ChatGPT/Gemini/Perplexity/Claude live (that needs a mention-index
vendor). It scores likely mention/citation from the business profile + GSC pages,
then returns errors with a clickable suggestion and a concrete solution.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import CmsConnection, FeatureRecord, User
from app.openai_client import chat_text, openai_configured

LIVE = "google-live-v1"

ENGINES = [
    {"id": "chatgpt", "name": "ChatGPT"},
    {"id": "gemini", "name": "Gemini"},
    {"id": "perplexity", "name": "Perplexity"},
    {"id": "claude", "name": "Claude"},
    {"id": "copilot", "name": "Copilot"},
    {"id": "aio", "name": "Google AI Overviews"},
    {"id": "grok", "name": "Grok"},
]


def _latest_payload(db: Session, user_id: int, kind: str) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if not row:
        return {}
    payload = dict(row.payload or {})
    seed = str(payload.get("seedVersion") or "")
    source = str(payload.get("source") or "")
    if seed != LIVE and not source.startswith("google") and source != "searchify_operator":
        return {}
    return payload


def _upsert(db: Session, user_id: int, kind: str, title: str, payload: dict):
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind, FeatureRecord.title == title)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if row is None:
        db.add(FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload, status="stored"))
    else:
        row.payload = payload
        row.status = "stored"
        flag_modified(row, "payload")
    db.commit()


def _site_url(db: Session, user_id: int, profile: dict) -> str:
    cms = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user_id, CmsConnection.status == "connected")
        .order_by(CmsConnection.id.desc())
        .first()
    )
    for raw in (profile.get("site") or "", getattr(cms, "site_url", "") if cms else "",):
        value = str(raw or "").strip()
        if value.startswith("http"):
            return value if value.endswith("/") else f"{value}/"
    pages = _latest_payload(db, user_id, "top-pages").get("rows") or []
    if pages and str(pages[0][0] if pages[0] else "").startswith("http"):
        return str(pages[0][0]).rstrip("/") + "/"
    return ""


def build_prompts(profile: dict, queries: list, pages: list) -> list[str]:
    biz = (profile.get("business") or "").strip() or "the business"
    services = (profile.get("services") or "").split(",")[0].strip() or "this service"
    area = (profile.get("areas") or profile.get("locations") or "").split(",")[0].strip() or "this area"
    q1 = str(queries[0][0]).strip() if queries else ""
    prompts = [
        f"Who offers {services} in {area}?",
        f"What is the best {services} company in {area}?",
        f"How do I choose a provider for {services}?",
        f"Compare companies that offer {services} near {area}.",
    ]
    if q1 and q1.lower() not in " ".join(prompts).lower():
        prompts[2] = q1
    if biz:
        prompts.append(f"Is {biz} a good option for {services}?")
    return prompts[:5]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _score_engine(checks: list[dict], engine_name: str) -> dict:
    rows = [c for c in checks if c.get("engine") == engine_name and c.get("mention") is not None]
    if not rows:
        return {"id": engine_name.lower(), "name": engine_name, "score": None, "mentions": 0, "citations": 0, "gaps": 0}
    mentions = sum(1 for c in rows if c.get("mention"))
    citations = sum(1 for c in rows if c.get("citation"))
    gaps = sum(1 for c in rows if c.get("status") != "ok")
    score = int(round(((mentions * 0.6) + (citations * 0.4)) / max(len(rows), 1) * 100))
    return {
        "id": next((e["id"] for e in ENGINES if e["name"] == engine_name), engine_name.lower()),
        "name": engine_name,
        "score": min(100, score),
        "mentions": mentions,
        "citations": citations,
        "gaps": gaps,
    }


def _fallback_report(profile: dict, prompts: list[str], site: str, reason: str) -> dict:
    biz = (profile.get("business") or "").strip() or "your business"
    checks = []
    for engine in ENGINES:
        prompt = prompts[0] if prompts else f"Who should I hire in this area?"
        missing_profile = not (profile.get("business") and profile.get("services"))
        error = (
            "Business name and services are missing, so nothing could be estimated."
            if missing_profile
            else f"Not estimated for {engine['name']}: {reason}"
        )
        suggestion = "Complete business profile" if missing_profile else "Add a named entity FAQ"
        checks.append(
            {
                "id": f"{engine['id']}-0",
                "engine": engine["name"],
                "prompt": prompt,
                "mention": None,
                "citation": None,
                "status": "not_checked",
                "error": error,
                "suggestion": suggestion,
                "excerpt": reason,
                "solution": {
                    "title": suggestion,
                    "why": error,
                    "steps": [
                        "Name the business, service, and city in the first 160 characters of the homepage.",
                        "Add a short FAQ: who you are, where you work, what you sell.",
                        "Publish, then sync Search Console so answer engines can recrawl the page.",
                    ],
                    "targetUrl": site or "",
                    "titleDraft": f"{biz} | {profile.get('services') or 'Services'}",
                    "descriptionDraft": f"{biz} provides {profile.get('services') or 'services'} in {profile.get('areas') or profile.get('locations') or 'your area'}.",
                },
            }
        )
    engines = [_score_engine(checks, e["name"]) for e in ENGINES]
    return _pack(profile, prompts, engines, checks, model="rules", note=reason)


def _pack(profile: dict, prompts: list[str], engines: list, checks: list, *, model: str, note: str) -> dict:
    mentions = sum(1 for c in checks if c.get("mention"))
    citations = sum(1 for c in checks if c.get("citation"))
    errors = [c for c in checks if c.get("status") != "ok"]
    def est(value):
        return "Not estimated" if value is None else "Likely" if value else "Unlikely"

    rows = [[c.get("prompt"), c.get("engine"), est(c.get("mention")), est(c.get("citation")), c.get("excerpt") or ""] for c in checks]
    return {
        "summary": note,
        "columns": ["Prompt", "Engine", "Mention (estimate)", "Citation (estimate)", "Excerpt"],
        "rows": rows,
        "engines": engines,
        "checks": checks,
        "prompts": prompts,
        "kpis": [
            ["Estimated mentions", str(mentions)],
            ["Estimated citations", str(citations)],
            ["Issues", str(len(errors))],
            ["Engines", str(len(engines))],
        ],
        "generatedAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": "searchify_operator",
        "seedVersion": LIVE,
        "model": model,
        "live": False,
        "estimate": True,
        "method": "owned-data-likelihood",
        "profile": {
            "business": profile.get("business") or "",
            "services": profile.get("services") or "",
            "areas": profile.get("areas") or profile.get("locations") or "",
        },
    }


def _normalize_checks(raw_checks: list, prompts: list[str], profile: dict, site: str) -> list[dict]:
    biz = (profile.get("business") or "").strip() or "the business"
    out = []
    by_engine = {e["name"]: 0 for e in ENGINES}
    for item in raw_checks or []:
        if not isinstance(item, dict):
            continue
        engine = str(item.get("engine") or "").strip()
        match = next((e for e in ENGINES if e["name"].lower() == engine.lower() or e["id"] == engine.lower()), None)
        if not match:
            continue
        idx = by_engine[match["name"]]
        by_engine[match["name"]] = idx + 1
        mention = _as_bool(item.get("mention"))
        citation = _as_bool(item.get("citation"))
        status = str(item.get("status") or ("ok" if mention else "error")).lower()
        if status not in {"ok", "gap", "error"}:
            status = "error" if not mention else "ok"
        solution = item.get("solution") if isinstance(item.get("solution"), dict) else {}
        steps = solution.get("steps") if isinstance(solution.get("steps"), list) else []
        steps = [str(s).strip() for s in steps if str(s).strip()][:6]
        if not steps:
            steps = [
                f"State “{biz}” plus the service and city on the landing page H1 and title.",
                "Add 3 FAQ answers that match the prompt in plain language.",
                "Keep one canonical URL and resubmit it in Search Console.",
            ]
        error = str(item.get("error") or "").strip() or (
            f"{match['name']} does not mention {biz} for this prompt." if not mention else ""
        )
        suggestion = str(item.get("suggestion") or "").strip() or ("Keep this page current" if mention else "Fix entity copy")
        out.append(
            {
                "id": str(item.get("id") or f"{match['id']}-{idx}"),
                "engine": match["name"],
                "prompt": str(item.get("prompt") or (prompts[0] if prompts else "")).strip(),
                "mention": mention,
                "citation": citation,
                "status": status,
                "error": error,
                "suggestion": suggestion,
                "excerpt": str(item.get("excerpt") or "")[:800],
                "solution": {
                    "title": str(solution.get("title") or suggestion)[:120],
                    "why": str(solution.get("why") or error)[:400],
                    "steps": steps,
                    "targetUrl": str(solution.get("targetUrl") or site or "")[:500],
                    "titleDraft": str(solution.get("titleDraft") or "")[:70],
                    "descriptionDraft": str(solution.get("descriptionDraft") or "")[:170],
                },
            }
        )
    if not out:
        return _fallback_report(profile, prompts, site, "Model returned no engine rows.").get("checks") or []
    missing = [e for e in ENGINES if not any(c["engine"] == e["name"] for c in out)]
    if missing:
        extra = _fallback_report(profile, prompts, site, "No row for this engine.")["checks"]
        for engine in missing:
            row = next((c for c in extra if c["engine"] == engine["name"]), None)
            if row:
                out.append(row)
    return out


def run_visibility_report(db: Session, user: User, profile: dict) -> dict:
    organic = _latest_payload(db, user.id, "organic-search")
    pages = _latest_payload(db, user.id, "top-pages").get("rows") or []
    queries = organic.get("rows") or []
    site = _site_url(db, user.id, profile)
    prompts = build_prompts(profile, queries, pages)
    page_bits = "\n".join(f"- {p[0][:120]} clicks={p[1] if len(p) > 1 else '—'}" for p in pages[:8])
    query_bits = "\n".join(f"- {q[0]}" for q in queries[:10])
    biz = profile.get("business") or ""
    services = profile.get("services") or ""
    areas = profile.get("areas") or profile.get("locations") or ""

    if not openai_configured():
        report = _fallback_report(
            profile,
            prompts,
            site,
            "OpenAI is not configured, so this is a rules-based gap report — not a live crawl of ChatGPT or Gemini.",
        )
        _save(db, user.id, report)
        return report

    engine_names = ", ".join(e["name"] for e in ENGINES)
    system = (
        "You are Searchify AI Visibility. Score whether popular answer engines are likely to mention "
        "and cite this business. Use ONLY the profile and Search Console facts provided. "
        "Do not invent live mention counts, ranks, or traffic. "
        "Return JSON only, no markdown."
    )
    user_msg = (
        f"Business: {biz}\nServices: {services}\nLocations: {areas}\nSite: {site or '(unknown)'}\n\n"
        f"GSC queries:\n{query_bits or '(none — Google Search Console not synced)'}\n\n"
        f"Top pages:\n{page_bits or '(none)'}\n\n"
        f"Prompts to evaluate:\n" + "\n".join(f"- {p}" for p in prompts) + "\n\n"
        f"Engines: {engine_names}\n"
        "For EACH engine produce 1-2 checks against the prompts. "
        "If the brand is not clearly findable from the facts, status=error, mention=false, citation=false. "
        "Each error needs a short suggestion label and a solution with title, why, 3 steps, "
        "targetUrl (prefer the homepage or a real GSC page), titleDraft (<=60 chars), descriptionDraft (<=155 chars).\n"
        "JSON shape:\n"
        '{"engines":[{"id":"chatgpt","score":0-100,"mentioned":false,"cited":false,"summary":"..."}],'
        '"checks":[{"id":"chatgpt-0","engine":"ChatGPT","prompt":"...","mention":false,"citation":false,'
        '"status":"error","error":"...","suggestion":"...","excerpt":"...",'
        '"solution":{"title":"...","why":"...","steps":["..."],"targetUrl":"...","titleDraft":"...","descriptionDraft":"..."}}]}'
    )
    try:
        text, model, _role = chat_text(
            system=system,
            user=user_msg,
            kind="ai-visibility",
            writing_type="AI visibility report",
            tokens=900,
            force_astra=False,
            max_tokens=2800,
        )
    except Exception as exc:  # noqa: BLE001
        report = _fallback_report(profile, prompts, site, f"Visibility model failed: {exc}"[:240])
        _save(db, user.id, report)
        return report

    parsed = _parse_json(text)
    checks = _normalize_checks(parsed.get("checks") or [], prompts, profile, site)
    engines = [_score_engine(checks, e["name"]) for e in ENGINES]
    for item in parsed.get("engines") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        row = next((e for e in engines if e["name"].lower() == name.lower() or e["id"] == str(item.get("id") or "").lower()), None)
        if row and item.get("score") is not None:
            try:
                row["score"] = max(0, min(100, int(item["score"])))
            except (TypeError, ValueError):
                pass
            if item.get("summary"):
                row["summary"] = str(item["summary"])[:240]
    report = _pack(
        profile,
        prompts,
        engines,
        checks,
        model=model,
        note="Likelihood report from your profile + Search Console. Not a live crawl of ChatGPT, Gemini, Perplexity, or Claude.",
    )
    _save(db, user.id, report)
    return report


def _save(db: Session, user_id: int, report: dict):
    title = "AI Visibility Report"
    _upsert(db, user_id, "ai-visibility-report", title, report)
    _upsert(db, user_id, "ai-visibility", title, report)


def latest_report(db: Session, user_id: int) -> dict:
    payload = _latest_payload(db, user_id, "ai-visibility-report") or _latest_payload(db, user_id, "ai-visibility")
    return payload
