"""OpenAI enrichment from live GSC/GA4 facts — topics, keywords, prompts, narratives."""

from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import FeatureRecord
from app.openai_client import chat_text, openai_configured

LIVE = "google-live-v1"


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
    db.commit()


def _latest_rows(db: Session, user_id: int, kind: str) -> list[list]:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if not row:
        return []
    payload = row.payload or {}
    seed = str(payload.get("seedVersion") or "")
    source = str(payload.get("source") or "")
    if seed != LIVE and not source.startswith("google") and source != "searchify_operator":
        return []
    return list(payload.get("rows") or [])


def _parse_table(text: str) -> list[list]:
    rows = []
    for line in text.splitlines():
        line = line.strip().strip("|")
        if not line or set(line) <= {"-", "|", " ", ":"}:
            continue
        if "---" in line:
            continue
        parts = [p.strip() for p in re.split(r"\s*\|\s*|\t", line) if p.strip()]
        if len(parts) >= 2 and parts[0].lower() not in {"cluster", "topic", "keyword", "prompt", "idea"}:
            rows.append(parts)
        elif len(parts) >= 2 and parts[0].lower() in {"cluster", "topic", "keyword", "prompt", "idea"}:
            continue
        elif len(parts) >= 2:
            rows.append(parts)
    return rows[:40]


def enrich_from_google(db: Session, user_id: int) -> dict:
    """Build Strong/Partial AI layers from current GSC/GA4 feature rows."""
    done = {
        "topics": False,
        "keywords": False,
        "prompts": False,
        "narratives": False,
        "openai": openai_configured(),
    }
    queries = _latest_rows(db, user_id, "organic-search")
    pages = _latest_rows(db, user_id, "top-pages")
    traffic = _latest_rows(db, user_id, "traffic-analytics")
    query_seed = [r[0] for r in queries[:25] if r]
    if not query_seed:
        return {**done, "note": "No GSC queries yet — Sync Google first"}

    seed_block = "\n".join(f"- {q}" for q in query_seed)

    if openai_configured():
        try:
            text, model, role = chat_text(
                system=(
                    "You cluster Search Console queries into SEO topic clusters. "
                    "Return a plain table with columns: Cluster | Sample queries | Intent | Priority. "
                    "No invented volumes. Max 12 rows."
                ),
                user=f"GSC queries:\n{seed_block}",
                kind="topic-research",
                tokens=600,
                max_tokens=1200,
            )
            rows = _parse_table(text)
            inferred = False
            if not rows:
                # Honest GSC mirror — never invent cluster labels when the model returns nothing.
                rows = [[q.split()[0].title() if q.split() else q, q, "Informational", "Medium"] for q in query_seed[:10]]
                inferred = True
            payload = {
                "summary": (
                    f"Topic clusters from live GSC queries ({model})."
                    if not inferred
                    else f"GSC query heads only — model returned no parseable table ({model})."
                ),
                "columns": ["Cluster", "Sample queries", "Intent", "Priority"],
                "rows": rows,
                "kpis": [["Clusters", str(len(rows))], ["Seed queries", str(len(query_seed))]],
                "source": "gsc_openai",
                "seedVersion": LIVE,
                "model": model,
                "role": role,
                "measured": False,
                "inferred": True,
                "dataKind": "gsc_topic_expansion",
                "provider": "openai+gsc",
            }
            _upsert(db, user_id, "topic-research", "Topic Research (GSC + Astra)", payload)
            _upsert(db, user_id, "topic-finder", "Topic Finder (GSC + Astra)", {**payload, "summary": "Topic ideas ranked from GSC query clusters."})
            done["topics"] = True
        except Exception as exc:  # noqa: BLE001
            done["topics_error"] = str(exc)[:160]

        try:
            text, model, role = chat_text(
                system=(
                    "Expand Search Console queries into keyword ideas. "
                    "Return table: Keyword | Angle | Parent query | Notes. "
                    "Do not invent search volume numbers. Max 30 rows."
                ),
                user=f"Seed queries:\n{seed_block}",
                kind="generate-keywords",
                tokens=700,
                max_tokens=1400,
            )
            rows = _parse_table(text)
            model_empty = False
            if not rows:
                # Mirror GSC only — do not invent “guide” variants as research keywords.
                rows = [[q, "GSC seed", q, "Model returned no expansions"] for q in query_seed[:20]]
                model_empty = True
            payload = {
                "summary": (
                    f"GSC queries only — model returned no parseable expansions ({model}). No invented volumes."
                    if model_empty
                    else f"Keyword ideas expanded from GSC with {model} (no third-party volume)."
                ),
                "columns": ["Keyword", "Angle", "Parent query", "Notes"],
                "rows": rows,
                "kpis": [["Ideas", str(len(rows))], ["From GSC", str(len(query_seed))]],
                "source": "gsc_openai",
                "seedVersion": LIVE,
                "model": model,
                "role": role,
                "measured": False,
                "inferred": not model_empty,
                "dataKind": "gsc_keyword_expansion" if not model_empty else "gsc_query_mirror",
                "provider": "openai+gsc",
                "metricsNote": "No search volume, difficulty, or CPC — GSC/OpenAI expansion only.",
            }
            _upsert(db, user_id, "generate-keywords", "Generate Keywords (GSC + OpenAI)", payload)
            done["keywords"] = True
        except Exception as exc:  # noqa: BLE001
            done["keywords_error"] = str(exc)[:160]

        try:
            text, model, role = chat_text(
                system=(
                    "Suggest prompts people might ask AI assistants about this site's topics. "
                    "Return table: Prompt | Related GSC query | Why it matters. Max 20 rows. No Brand Radar claims."
                ),
                user=f"GSC queries:\n{seed_block}",
                kind="prompt-research",
                tokens=500,
                max_tokens=1000,
            )
            rows = _parse_table(text)
            if not rows:
                # Skip inventing “What is the best …?” prompts when the model fails.
                done["prompts"] = False
                done["prompts_error"] = "Model returned no parseable prompt table"
            else:
                payload = {
                    "summary": f"AI prompt suggestions from GSC ({model}). Not a live ChatGPT mention index.",
                    "columns": ["Prompt", "Related GSC query", "Why it matters"],
                    "rows": rows,
                    "source": "gsc_openai",
                    "seedVersion": LIVE,
                    "model": model,
                    "role": role,
                    "measured": False,
                    "inferred": True,
                    "observed": False,
                    "dataKind": "gsc_prompt_suggestions",
                    "provider": "openai+gsc",
                    "disclaimer": "Suggested prompts for future tracking — not measured AI-search citations.",
                }
                _upsert(db, user_id, "prompt-research", "Prompt Research (GSC + OpenAI)", payload)
                _upsert(
                    db,
                    user_id,
                    "prompt-tracking",
                    "Prompt Tracking (suggested set)",
                    {
                        **payload,
                        "summary": "Suggested prompt set to track over time (manual / future schedule).",
                        "columns": ["Prompt", "Status", "Source"],
                        "rows": [[r[0], "Queued", "GSC+OpenAI"] for r in rows],
                    },
                )
                done["prompts"] = True
        except Exception as exc:  # noqa: BLE001
            done["prompts_error"] = str(exc)[:160]

        try:
            page_bits = "\n".join(f"- {p[0][:80]} clicks={p[1]}" for p in pages[:10])
            traffic_bits = "\n".join(f"- {t[0]}: {t[1]}" for t in traffic[:8])
            text, model, role = chat_text(
                system=(
                    "Write a concise AI visibility narrative for an SEO dashboard. "
                    "Use only the provided GSC/GA4 facts. Do not invent ChatGPT mention counts. "
                    "Return plain text with short sections: Summary, Opportunities, Risks, Next actions."
                ),
                user=f"Queries:\n{seed_block}\n\nTop pages:\n{page_bits}\n\nTraffic:\n{traffic_bits}",
                kind="ai-analysis",
                tokens=700,
                force_astra=True,
                max_tokens=1600,
            )
            narrative_rows = [
                ["Summary", text.split("\n")[0][:120], "OpenAI"],
                ["Generated", datetime.utcnow().isoformat(timespec="seconds"), model],
                ["Queries used", str(len(query_seed)), "GSC"],
                ["Pages used", str(len(pages[:10])), "GSC"],
            ]
            payload = {
                "summary": "AI analysis narrative from owned GSC/GA4 data (no live mention index).",
                "columns": ["Section", "Detail", "Source"],
                "rows": narrative_rows,
                "draft": text,
                "source": "gsc_ga4_openai",
                "seedVersion": LIVE,
                "model": model,
                "role": role,
                "measured": False,
                "inferred": True,
                "observed": False,
                "live": False,
                "estimate": False,
                "dataKind": "gsc_ga4_narrative",
                "provider": "openai+gsc+ga4",
                "disclaimer": "Narrative analysis of owned Google data — not a live ChatGPT/Gemini/Perplexity measurement.",
            }
            # Never overwrite the operator AI-visibility measurement/estimate report with a GSC narrative.
            _upsert(db, user_id, "ai-analysis", "AI Analysis (owned data)", payload)
            _upsert(db, user_id, "brand-narrative", "Brand Narrative", {**payload, "summary": "Narrative draft from GSC/GA4 facts."})
            done["narratives"] = True
        except Exception as exc:  # noqa: BLE001
            done["narratives_error"] = str(exc)[:160]
    else:
        # Deterministic fallback without OpenAI
        rows = [[f"Topic: {q.split()[0]}", q, "Informational", "Medium"] for q in query_seed[:10]]
        _upsert(
            db,
            user_id,
            "topic-research",
            "Topic Research (GSC)",
            {
                "summary": "Basic clusters from GSC query heads (OpenAI off).",
                "columns": ["Cluster", "Sample queries", "Intent", "Priority"],
                "rows": rows,
                "source": "gsc",
                "seedVersion": LIVE,
            },
        )
        _upsert(
            db,
            user_id,
            "generate-keywords",
            "Generate Keywords (GSC)",
            {
                "summary": "Keyword list mirrored from GSC (OpenAI off).",
                "columns": ["Keyword", "Angle", "Parent query", "Notes"],
                "rows": [[q, "GSC seed", q, "Connect OpenAI to expand"] for q in query_seed],
                "source": "gsc",
                "seedVersion": LIVE,
            },
        )
        done["topics"] = True
        done["keywords"] = True

    return done
