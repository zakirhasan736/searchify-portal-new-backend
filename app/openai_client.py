"""OpenAI chat helper for Searchify drafts. Astra is primary; Luna only for short fast jobs."""

from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from app import config

log = logging.getLogger(__name__)

# Which chat models this OpenAI project may use. Asked once, then cached, so a key without access
# to the newest models still works with the best model it does have instead of failing every call.
MODELS_TTL = 1800
DENIED_TTL = 3600
_lock = threading.Lock()
_models: dict = {"at": 0.0, "ids": None}
_denied: dict[str, float] = {}
# Best first. Used only when none of the role's own models are available to the project.
ANY_CHAT = [
    r"^gpt-6-(astra|sol)", r"^gpt-6", r"^gpt-5\.\d+-(sol|terra)", r"^gpt-5\.\d+", r"^gpt-5(?!-nano)",
    r"^gpt-4\.1$", r"^gpt-4o$", r"^gpt-4\.1-mini", r"^gpt-4o-mini", r"^gpt-5-nano", r"^gpt-4\.1-nano",
]
NOT_CHAT = re.compile(r"audio|realtime|transcribe|tts|image|search|embedding|moderation|instruct|codex|preview", re.I)


def available_models(*, refresh: bool = False) -> set[str] | None:
    """Model ids the key can use, or None when OpenAI could not be asked (then every model is tried)."""
    if not config.OPENAI_API_KEY:
        return None
    with _lock:
        if not refresh and _models["ids"] is not None and time.time() - _models["at"] < MODELS_TTL:
            return _models["ids"]
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
        if response.status_code >= 400:
            log.warning("openai_models_list_failed", extra={"status": response.status_code})
            return None
        ids = {str(row.get("id") or "") for row in response.json().get("data") or []} - {""}
    except Exception as exc:  # noqa: BLE001 - listing is a hint; never block a call on it
        log.warning("openai_models_list_failed", extra={"error": str(exc)[:160]})
        return None
    with _lock:
        _models.update(at=time.time(), ids=ids)
    return ids


def _best_available(ids: set[str]) -> list[str]:
    chat = sorted((m for m in ids if m.startswith(("gpt-", "o")) and not NOT_CHAT.search(m)), reverse=True)
    out: list[str] = []
    for pattern in ANY_CHAT:
        out += [m for m in chat if re.search(pattern, m) and m not in out]
    return out[:3]


def _usable(chain: list[str]) -> list[str]:
    """Drop models the project cannot use; add the best model it can use when none are left."""
    now = time.time()
    chain = [m for m in chain if _denied.get(m, 0) < now]
    ids = available_models()
    if ids is None:
        return chain
    kept = [m for m in chain if m in ids]
    return kept or _best_available(ids)

# First model is always tried first. Astra leads for serious roles.
FALLBACKS = {
    "astra": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.2", "gpt-4.1"],
    "writer": ["gpt-5.6-sol", "gpt-6-astra", "gpt-5.6-terra", "gpt-5.2", "gpt-4.1"],
    "seo": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o"],
    "analyst": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.2", "gpt-4.1"],
    "fast": ["gpt-5.6-luna", "gpt-4o-mini", "gpt-4o"],
    "assistant": ["gpt-5.6-terra"],
    "vision": ["gpt-4o", "gpt-4.1"],
}


def openai_configured() -> bool:
    return bool(config.OPENAI_API_KEY)


def _candidates(
    kind: str | None,
    *,
    writing_type: str | None = None,
    brief: str | None = None,
    tokens: int | None = None,
    force_astra: bool = False,
) -> list[str]:
    role = config.resolve_openai_role(
        kind,
        writing_type=writing_type,
        brief=brief,
        tokens=tokens,
        force_astra=force_astra,
    )
    primary = config.openai_model_for(kind=kind, role=role)
    # If role escalated to Astra, force Astra chain even when kind was "fast"
    chain_key = role if role in FALLBACKS else "astra"
    chain = [primary, *FALLBACKS[chain_key]]
    # When Astra is required, never fall back to Luna first — keep Astra at front
    if role == "astra" and config.OPENAI_MODEL_ASTRA:
        chain = [config.OPENAI_MODEL_ASTRA, *[m for m in chain if m != config.OPENAI_MODEL_ASTRA]]
    seen: set[str] = set()
    out: list[str] = []
    for model in chain:
        if model and model not in seen:
            seen.add(model)
            out.append(model)
    return out


def chat_text(
    *,
    system: str,
    user: str,
    kind: str | None = None,
    writing_type: str | None = None,
    brief: str | None = None,
    tokens: int | None = None,
    force_astra: bool = False,
    max_tokens: int = 2500,
) -> tuple[str, str, str]:
    """
    Returns (text, model_used, role_used).
    Raises RuntimeError if no key or all models fail.
    """
    if not openai_configured():
        raise RuntimeError("OPENAI_API_KEY is not set")

    role = config.resolve_openai_role(
        kind,
        writing_type=writing_type,
        brief=brief,
        tokens=tokens,
        force_astra=force_astra,
    )
    last_error = "unknown"
    wanted = _candidates(
        kind,
        writing_type=writing_type,
        brief=brief,
        tokens=tokens,
        force_astra=force_astra,
    )
    denied: list[str] = []
    models = _usable(wanted)
    if not models:
        raise RuntimeError(
            "OpenAI request failed (this OpenAI project has no chat model Searchify can use. "
            "Allow a model such as gpt-4o or gpt-4.1 under Project → Limits on platform.openai.com, or set OPENAI_MODEL_DEFAULT)"
        )
    for model in models:
        try:
            payload: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_completion_tokens": max_tokens,
            }
            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code >= 400:
                no_access = "does not have access to model" in response.text or "model_not_found" in response.text
                if no_access:
                    denied.append(model)
                    _denied[model] = time.time() + DENIED_TTL
                    last_error = f"{model}: {response.status_code} {response.text[:240]}"
                    continue
                alt = {
                    "model": model,
                    "messages": payload["messages"],
                    "max_tokens": max_tokens,
                }
                with httpx.Client(timeout=120.0) as client:
                    response = client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                            "Content-Type": "application/json",
                        },
                        json=alt,
                    )
            if response.status_code >= 400:
                last_error = f"{model}: {response.status_code} {response.text[:240]}"
                continue
            data = response.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not text.strip():
                last_error = f"{model}: empty content"
                continue
            return text.strip(), model, role
        except Exception as exc:  # noqa: BLE001
            last_error = f"{model}: {exc}"
            continue
    if denied and len(denied) == len(models):
        raise RuntimeError(
            f"OpenAI request failed (this OpenAI project has no access to {', '.join(denied)}. "
            "Allow one of them under Project → Limits on platform.openai.com, or set OPENAI_MODEL_DEFAULT to a model the project has)"
        )
    raise RuntimeError(f"OpenAI request failed ({last_error})")


def generate_draft_body(
    kind: str,
    brief: str,
    *,
    writing_type: str | None = None,
    tokens: int | None = None,
    force_astra: bool = False,
) -> tuple[str, str, str]:
    system = (
        "You are Searchify, an SEO and content studio. "
        "Write practical, publishable draft text for marketers. "
        "Do not invent live traffic, rank, or backlink numbers. "
        "If facts are missing, use placeholders like [keyword] or note that GSC/GA4 is not connected. "
        "Return plain text with clear headings — no markdown code fences."
    )
    user = (
        f"Draft kind: {kind or 'article'}\n"
        f"Writing type: {writing_type or 'Article'}\n"
        f"Brief / instructions:\n{brief.strip() or 'Write a short SEO-ready outline for a marketing page.'}\n\n"
        "Produce the draft now."
    )
    try:
        token_n = int(tokens or 800)
    except (TypeError, ValueError):
        token_n = 800
    max_out = min(max(token_n * 2, 800), 4000)
    return chat_text(
        system=system,
        user=user,
        kind=kind,
        writing_type=writing_type,
        brief=brief,
        tokens=token_n,
        force_astra=force_astra,
        max_tokens=max_out,
    )
