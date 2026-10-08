"""OpenAI chat helper for Searchify drafts. Astra is primary; Luna only for short fast jobs."""

from __future__ import annotations

import httpx

from app import config

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
    for model in _candidates(
        kind,
        writing_type=writing_type,
        brief=brief,
        tokens=tokens,
        force_astra=force_astra,
    ):
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
                denied = "does not have access to model" in response.text or "model_not_found" in response.text
                if denied:
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
