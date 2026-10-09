"""Build DataForSEO AI Optimization `llm_responses/live` requests that each engine accepts.

The four endpoints do not share one schema. Sending a field an endpoint does not document returns
`40501 Invalid Field` for the whole task, so every request is built from that engine's own field list.

Field lists come from the DataForSEO v3 docs (ai_optimization/<engine>/llm_responses/live):
- chat_gpt: web_search, force_web_search, web_search_country_iso_code, web_search_city.
  Country and city need web_search=true and are not supported by o3-mini, o1, o1-pro.
- gemini: web_search only. No country or city field.
- perplexity: no web_search field (Sonar models always search). Country only for sonar models.
- claude: web_search, force_web_search, web_search_country_iso_code (fixed list), web_search_city.
- max_output_tokens: 1..4096, at least 1024 for reasoning models.
"""

from __future__ import annotations

import re

ENGINES = {"ChatGPT": "chat_gpt", "Gemini": "gemini", "Perplexity": "perplexity", "Claude": "claude"}

COMMON = {"user_prompt", "model_name", "max_output_tokens", "temperature", "top_p", "system_message", "tag"}
FIELDS = {
    "chat_gpt": COMMON | {"web_search", "force_web_search", "web_search_country_iso_code", "web_search_city"},
    "gemini": COMMON | {"web_search"},
    "perplexity": COMMON | {"web_search_country_iso_code"},
    "claude": COMMON | {"web_search", "force_web_search", "web_search_country_iso_code", "web_search_city"},
}
CHATGPT_NO_LOCATION = {"o3-mini", "o1", "o1-pro"}
CLAUDE_COUNTRIES = {
    "AR", "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CN", "DE", "DK", "ES", "FI", "FR", "GB", "HK", "ID", "IN",
    "IT", "JP", "KR", "MX", "MY", "NL", "NO", "NZ", "PH", "PL", "PT", "RU", "SA", "SE", "TR", "TW", "US", "ZA",
}
PREFERRED = {
    "chat_gpt": ("gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"),
    "gemini": ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"),
    "perplexity": ("sonar",),
    "claude": ("claude-haiku-4-5", "claude-3-5-haiku"),
}
PROMPT_LIMIT = 500
TOKENS = {"plain": 800, "reasoning": 1024}


class RequestError(ValueError):
    """The request cannot be built for this engine. Raised before any paid call."""


def pick_model(engine: str, models: list[dict]) -> dict:
    """Choose a cheap model that can search the web. Returns {name, reasoning, webSearch}."""
    rows = [m for m in models or [] if isinstance(m, dict) and m.get("model_name")]
    if not rows:
        raise RequestError(f"No {engine} model is available on the DataForSEO account.")
    by_name = {m["model_name"]: m for m in rows}

    def meta(m: dict) -> dict:
        web = m.get("web_search_supported")
        if web is None:
            web = engine == "perplexity"
        return {"name": m["model_name"], "reasoning": bool(m.get("reasoning")), "webSearch": bool(web)}

    for name in PREFERRED.get(engine, ()):
        if name in by_name and (engine == "perplexity" or by_name[name].get("web_search_supported")):
            return meta(by_name[name])
    searchable = [m for m in rows if m.get("web_search_supported") or engine == "perplexity"] or rows
    plain = [m for m in searchable if not m.get("reasoning")] or searchable
    cheap = [m for m in plain if re.search(r"mini|flash|haiku|^sonar$", m["model_name"], re.I)] or plain
    return meta(sorted(cheap, key=lambda m: len(m["model_name"]))[0])


def build(engine: str, *, prompt: str, model: dict, iso: str = "", city: str = "") -> tuple[dict, dict]:
    """(payload, applied) for one engine. `applied` records which location fields were actually sent."""
    if engine not in FIELDS:
        raise RequestError(f"Unknown AI engine: {engine}")
    text = " ".join(str(prompt or "").split())
    if not text:
        raise RequestError("The prompt is empty.")
    if len(text) > PROMPT_LIMIT:
        raise RequestError(f"The prompt is {len(text)} characters. The limit is {PROMPT_LIMIT}.")
    name = str((model or {}).get("name") or "").strip()
    if not name:
        raise RequestError(f"No {engine} model was chosen.")
    reasoning = bool(model.get("reasoning"))
    web = bool(model.get("webSearch"))
    iso = (iso or "").strip().upper()
    city = (city or "").strip()

    payload: dict = {
        "user_prompt": text,
        "model_name": name,
        "max_output_tokens": TOKENS["reasoning"] if reasoning else TOKENS["plain"],
    }
    applied = {"country": "", "city": "", "webSearch": False, "note": ""}

    if engine == "chat_gpt":
        if web:
            payload["web_search"] = True
            applied["webSearch"] = True
            if iso and name not in CHATGPT_NO_LOCATION:
                payload["web_search_country_iso_code"] = iso
                applied["country"] = iso
                if city:
                    payload["web_search_city"] = city
                    applied["city"] = city
        else:
            applied["note"] = f"{name} cannot search the web, so the answer is from the model alone."
    elif engine == "gemini":
        if web:
            payload["web_search"] = True
            applied["webSearch"] = True
        if iso:
            applied["note"] = "Gemini does not accept a search location, so the answer is not country-specific."
    elif engine == "perplexity":
        applied["webSearch"] = True
        if iso and name.startswith("sonar"):
            payload["web_search_country_iso_code"] = iso
            applied["country"] = iso
    elif engine == "claude":
        if web:
            payload["web_search"] = True
            applied["webSearch"] = True
            if iso in CLAUDE_COUNTRIES:
                payload["web_search_country_iso_code"] = iso
                applied["country"] = iso
                if city:
                    payload["web_search_city"] = city
                    applied["city"] = city
            elif iso:
                applied["note"] = f"Claude does not support search location {iso}, so the answer is not country-specific."

    unknown = set(payload) - FIELDS[engine]
    if unknown:
        raise RequestError(f"{engine} does not accept: {', '.join(sorted(unknown))}")
    return payload, applied
