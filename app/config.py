import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://searchify:searchify@127.0.0.1:55432/searchify",
)
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-local-secret")
JWT_HOURS = int(os.getenv("JWT_HOURS", "12"))
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "http://localhost:3000").rstrip("/")
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]

# OpenAI — Sol is default quality; Luna for short/fast; Astra only for hard long-form.
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL_ASTRA = os.getenv("OPENAI_MODEL_ASTRA", "gpt-6-astra").strip()
OPENAI_MODEL_DEFAULT = os.getenv("OPENAI_MODEL_DEFAULT", "gpt-6-sol").strip()
OPENAI_MODEL_WRITER = os.getenv("OPENAI_MODEL_WRITER", "gpt-6-sol").strip()
OPENAI_MODEL_ANALYST = os.getenv("OPENAI_MODEL_ANALYST", "gpt-6-sol").strip()
OPENAI_MODEL_FAST = os.getenv("OPENAI_MODEL_FAST", "gpt-6-luna").strip()
OPENAI_MODEL_VISION = os.getenv("OPENAI_MODEL_VISION", "gpt-4o").strip()

GOOGLE_OAUTH_CLIENT_ID = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
GOOGLE_OAUTH_REDIRECT_URI = (
    os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or "http://127.0.0.1:8000/api/v1/oauth/google/callback"
).strip()
GOOGLE_PLACES_API_KEY = (os.getenv("GOOGLE_PLACES_API_KEY") or "").strip()
GOOGLE_PAGESPEED_API_KEY = (os.getenv("GOOGLE_PAGESPEED_API_KEY") or "").strip()
GOOGLE_ADS_DEVELOPER_TOKEN = (os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN") or "").strip()
GOOGLE_ADS_LOGIN_CUSTOMER_ID = (os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "").replace("-", "").strip()
GOOGLE_ADS_API_VERSION = (os.getenv("GOOGLE_ADS_API_VERSION") or "v19").strip() or "v19"

# Login OAuth (Google sign-in + GitHub). Login redirect can differ from GSC connect callback.
GOOGLE_LOGIN_REDIRECT_URI = (
    os.getenv("GOOGLE_LOGIN_REDIRECT_URI")
    or "http://127.0.0.1:8000/api/v1/auth/oauth/google/callback"
).strip()
GITHUB_CLIENT_ID = (os.getenv("GITHUB_CLIENT_ID") or "").strip()
GITHUB_CLIENT_SECRET = (os.getenv("GITHUB_CLIENT_SECRET") or "").strip()
GITHUB_OAUTH_REDIRECT_URI = (
    os.getenv("GITHUB_OAUTH_REDIRECT_URI")
    or "http://127.0.0.1:8000/api/v1/auth/oauth/github/callback"
).strip()

# Optional — competitor SERP titles for work-queue meta. Leave blank to skip.
DATAFORSEO_LOGIN = (os.getenv("DATAFORSEO_LOGIN") or "").strip()
DATAFORSEO_PASSWORD = (os.getenv("DATAFORSEO_PASSWORD") or "").strip()

# Auto-sync connected Google data (hours between runs per account)
GOOGLE_AUTO_SYNC_ENABLED = (os.getenv("GOOGLE_AUTO_SYNC_ENABLED") or "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GOOGLE_AUTO_SYNC_HOURS = float(os.getenv("GOOGLE_AUTO_SYNC_HOURS") or "6")

# Base kind → role before escalation (fast | writer/sol | astra)
OPENAI_KIND_ROLES = {
    "article": "astra",
    "ai-article": "astra",
    "brief": "writer",
    "seo-brief": "writer",
    "seo-writing": "writer",
    "seo-content-template": "writer",
    "content-optimizer": "writer",
    "content-repurposing": "writer",
    "content-creation": "writer",
    "on-page-seo": "writer",
    "site-change": "writer",
    "report": "writer",
    "site-audit": "writer",
    "market-overview": "writer",
    "ai-analysis": "writer",
    "brand-narrative": "writer",
    "ads": "fast",
    "paid-search": "fast",
    "local-ai-agent": "fast",
    "local-reviews": "fast",
    "topic-finder": "fast",
    "topic-research": "fast",
    "generate-keywords": "fast",
    "meta": "fast",
    "grammar": "fast",
    "prompt-research": "fast",
    "ai-visibility": "writer",
    "assistant": "assistant",
}

# Kinds that must always use Astra (expensive — keep narrow for v1)
ASTRA_REQUIRED_KINDS = {
    "article",
    "ai-article",
}

ASTRA_TYPE_HINTS = {"article", "blog", "full blog", "long-form"}


def resolve_openai_role(
    kind: str | None = None,
    *,
    writing_type: str | None = None,
    brief: str | None = None,
    tokens: int | None = None,
    force_astra: bool = False,
) -> str:
    """
    Pick a model role for v1 cost control:
    - fast (Luna): short high-volume jobs
    - writer (Sol): default quality for operator / briefs / on-page
    - astra: long articles or force_astra only
    """
    kind_key = (kind or "").strip().lower()
    if kind_key == "assistant":
        return "assistant"
    # Title and description: gpt-6-sol, then gpt-5.6-terra, gpt-5.6-sol, and gpt-4o.
    # A long page brief must not escalate this job onto the general writer chain.
    if kind_key == "meta-seo":
        return "seo"
    type_key = (writing_type or "").strip().lower()
    brief_text = (brief or "").strip()
    token_n = int(tokens or 0)

    if force_astra or kind_key in ASTRA_REQUIRED_KINDS:
        return "astra"
    if type_key in ASTRA_TYPE_HINTS:
        return "astra"
    # Long jobs escalate to Sol (writer), not Astra — unless force_astra
    if token_n >= 1200 or len(brief_text) >= 1200:
        return "writer"
    if any(word in brief_text.lower() for word in ("full blog", "long-form article", "strategy report")):
        return "astra"

    base = OPENAI_KIND_ROLES.get(kind_key, "writer")
    if base in {"writer", "analyst", "sol"}:
        return "writer"
    return base


def openai_model_for(kind: str | None = None, role: str | None = None, **kwargs) -> str:
    resolved = (role or resolve_openai_role(kind, **kwargs)).lower()
    if resolved == "assistant":
        return "gpt-4o"
    if resolved == "seo":
        return OPENAI_MODEL_WRITER or "gpt-6-sol"
    if resolved == "fast":
        return OPENAI_MODEL_FAST
    if resolved == "vision":
        return OPENAI_MODEL_VISION
    if resolved in {"writer", "analyst", "sol"}:
        return OPENAI_MODEL_WRITER if resolved != "analyst" else OPENAI_MODEL_ANALYST
    if resolved == "astra":
        return OPENAI_MODEL_ASTRA
    return OPENAI_MODEL_DEFAULT
