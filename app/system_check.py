"""Live check of every outside service the SEO tools need, for the HQ admin.

Answers "why does a connected customer see nothing?" in one place: is DataForSEO set up and funded, which
DataForSEO APIs the account can use, which OpenAI models the key can call, and whether Google sign-in and
PageSpeed are configured. The quick check only uses free calls. The full check spends a few cents on one
tiny request per DataForSEO API so a missing subscription shows up before a customer finds it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import config, openai_client, research
from app.models import GoogleConnection

OK, WARN, ERROR = "ok", "warn", "error"


def _row(key: str, state: str, label: str, detail: str = "", fix: str = "") -> dict:
    return {"key": key, "state": state, "label": label, "detail": detail[:500], "fix": fix[:400]}


def _dfs_probe(key: str, label: str, method: str, path: str, payload: list | None) -> dict:
    try:
        task, cost = research._task(method, path, payload, timeout=40)
    except research.ResearchError as exc:
        tech = getattr(exc, "technical", "") or ""
        fix = ""
        if "40204" in tech or getattr(exc, "code", "") == "provider_api_disabled":
            fix = "Turn this API on (or start its subscription) in the DataForSEO dashboard under Plans and Subscriptions."
        elif getattr(exc, "code", "") in {"provider_balance", "provider_balance_low"}:
            fix = "Top up the DataForSEO balance."
        return _row(key, ERROR, label, f"{exc} {tech}".strip(), fix)
    return _row(key, OK, label, f"Answered (cost ${cost:.4f}).")


def dataforseo(deep: bool = False) -> list[dict]:
    rows: list[dict] = []
    if not research.configured():
        return [_row("dfs", ERROR, "DataForSEO is not configured",
                     "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are empty on the backend, so keywords, backlinks, the site audit and AI visibility cannot run.",
                     "Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD (API login and API password from the DataForSEO dashboard) and restart the backend.")]
    try:
        task, _ = research._task("GET", "/appendix/user_data", None, timeout=20)
    except research.ResearchError as exc:
        return [_row("dfs", ERROR, "DataForSEO rejected the login", f"{exc} {getattr(exc, 'technical', '')}",
                     "Check that the API login and API password (not the website password) are set on the backend.")]
    info = ((task.get("result") or [{}])[0]) or {}
    money = info.get("money") or {}
    balance = money.get("balance")
    try:
        funds = float(balance)
    except (TypeError, ValueError):
        funds = None
    if funds is None:
        rows.append(_row("dfs", OK, "DataForSEO connected", f"Login {info.get('login') or '?'}; balance not reported."))
    elif funds <= 0:
        rows.append(_row("dfs", ERROR, "DataForSEO balance is empty", f"Balance ${funds:.2f}.", "Top up the DataForSEO balance."))
    elif funds < 5:
        rows.append(_row("dfs", WARN, "DataForSEO balance is low", f"Balance ${funds:.2f}.", "Top up soon so research doesn't stop."))
    else:
        rows.append(_row("dfs", OK, "DataForSEO connected", f"Login {info.get('login') or '?'}; balance ${funds:.2f}."))
    expiry = info.get("backlinks_subscription_expiry_date")
    if "backlinks_subscription_expiry_date" in info:
        active = False
        if expiry:
            try:
                active = datetime.fromisoformat(str(expiry).replace(" +00:00", "+00:00").replace(" ", "T")).replace(tzinfo=None) > datetime.utcnow()
            except ValueError:
                active = True
        rows.append(_row("dfs_backlinks_sub", OK if active else ERROR, "Backlinks subscription",
                         f"Expires {expiry}." if expiry else "No Backlinks subscription on this DataForSEO account.",
                         "" if active else "The Backlinks screen needs DataForSEO's Backlinks API subscription. Start it in the DataForSEO dashboard."))
    if deep:
        rows.append(_dfs_probe("dfs_labs", "Keywords (DataForSEO Labs)", "POST", "/dataforseo_labs/google/keyword_overview/live",
                               [{"keywords": ["roof repair"], "location_name": "United States", "language_code": "en"}]))
        rows.append(_dfs_probe("dfs_backlinks", "Backlinks API", "POST", "/backlinks/summary/live", [{"target": "dataforseo.com"}]))
        rows.append(_dfs_probe("dfs_onpage", "Site audit (On-Page API)", "GET", "/on_page/tasks_ready", None))
        rows.append(_dfs_probe("dfs_ai", "AI visibility (AI Optimization API)", "GET", "/ai_optimization/chat_gpt/llm_responses/models", None))
        rows.append(_dfs_probe("dfs_serp", "Rank checks (SERP API)", "GET", "/serp/google/locations/us", None))
    return rows


def openai() -> list[dict]:
    if not config.OPENAI_API_KEY:
        return [_row("openai", ERROR, "OpenAI is not configured",
                     "OPENAI_API_KEY is empty, so the business read, keyword suggestions and title drafts cannot run.",
                     "Set OPENAI_API_KEY on the backend and restart it.")]
    ids = openai_client.available_models(refresh=True)
    if ids is None:
        return [_row("openai", ERROR, "OpenAI did not list models",
                     "The key may be wrong, revoked, or the project has no model access.",
                     "Check the key at platform.openai.com → API keys, and the project's model access under Limits.")]
    rows = []
    roles = {
        "Business read and analysis": [config.OPENAI_MODEL_ASTRA, *openai_client.FALLBACKS["astra"]],
        "Title and description drafts": [config.OPENAI_MODEL_WRITER, *openai_client.FALLBACKS["writer"]],
        "Short fast jobs": [config.OPENAI_MODEL_FAST, *openai_client.FALLBACKS["fast"]],
    }
    for label, chain in roles.items():
        chain = list(dict.fromkeys(m for m in chain if m))
        usable = [m for m in chain if m in ids]
        if usable:
            state = OK if usable[0] == chain[0] else WARN
            detail = f"Uses {usable[0]}." + ("" if state == OK else f" Preferred {chain[0]} is not available to this project.")
            rows.append(_row(f"openai_{label[:8].lower()}", state, label, detail))
        else:
            best = openai_client._best_available(ids)
            rows.append(_row(f"openai_{label[:8].lower()}", WARN if best else ERROR, label,
                             f"None of {', '.join(chain)} are available. " + (f"Falls back to {best[0]}." if best else "No chat model is available."),
                             "Allow the models under Project → Limits on platform.openai.com, or set OPENAI_MODEL_* to a model the project has."))
    return rows


def google(db: Session) -> list[dict]:
    rows = []
    if not (config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET):
        rows.append(_row("google_oauth", ERROR, "Google sign-in is not configured", "GOOGLE_OAUTH_CLIENT_ID / SECRET are empty.",
                         "Create an OAuth client in Google Cloud and set both values on the backend."))
    else:
        redirect = config.GOOGLE_OAUTH_REDIRECT_URI
        local = "127.0.0.1" in redirect or "localhost" in redirect
        rows.append(_row("google_oauth", WARN if local else OK, "Google sign-in configured",
                         f"Redirect URI {redirect}.", "Set GOOGLE_OAUTH_REDIRECT_URI to the live backend callback and add the same URI in Google Cloud." if local else ""))
    rows.append(_row("pagespeed", OK if config.GOOGLE_PAGESPEED_API_KEY else WARN, "PageSpeed key",
                     "Set." if config.GOOGLE_PAGESPEED_API_KEY else "Not set; PageSpeed calls share Google's tiny anonymous quota and often fail.",
                     "" if config.GOOGLE_PAGESPEED_API_KEY else "Create an API key with the PageSpeed Insights API enabled and set GOOGLE_PAGESPEED_API_KEY."))
    total = db.query(func.count(GoogleConnection.id)).scalar() or 0
    broken = (
        db.query(GoogleConnection.last_error, func.count(GoogleConnection.id))
        .filter(GoogleConnection.last_error != "")
        .group_by(GoogleConnection.last_error)
        .order_by(func.count(GoogleConnection.id).desc())
        .limit(3)
        .all()
    )
    if broken:
        n = sum(count for _e, count in broken)
        rows.append(_row("google_errors", WARN, f"{n} of {total} Google connections report an error",
                         " · ".join(f"{count}× {err[:140]}" for err, count in broken)))
    else:
        rows.append(_row("google_errors", OK, "Google connections", f"{total} connected, none reporting errors."))
    return rows


def run(db: Session, *, deep: bool = False) -> dict:
    checks = [*dataforseo(deep), *openai(), *google(db)]
    return {
        "ok": True,
        "deep": deep,
        "checkedAt": datetime.utcnow().isoformat(timespec="seconds"),
        "checks": checks,
        "errors": sum(1 for c in checks if c["state"] == ERROR),
        "warnings": sum(1 for c in checks if c["state"] == WARN),
    }

