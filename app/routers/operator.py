"""SEO Operator — opportunity → AI change → approve → CMS execute → monitor."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import cms_connectors
from app.database import get_db
from app.models import CmsConnection, Draft, FeatureRecord, GoogleConnection, Job, SiteChange, User
from app.routers.google_connect import _connection_payload
from app import ai_visibility
from app.openai_client import chat_text, generate_draft_body, openai_configured
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/operator", tags=["operator"])

LIVE = "google-live-v1"
PLAN_LIMITS = {"starter": 5, "agency": 10, "scale": 15}


def _homepage_url(*candidates: str) -> str:
    for raw in candidates:
        value = str(raw or "").strip()
        if not value:
            continue
        if value.startswith("sc-domain:"):
            value = "https://" + value.split(":", 1)[1]
        if value.startswith("//"):
            value = "https:" + value
        if not value.startswith("http://") and not value.startswith("https://"):
            if "." in value and " " not in value:
                value = "https://" + value.lstrip("/")
            else:
                continue
        return value if value.endswith("/") else f"{value}/"
    return ""


def _page_url(raw, homepage: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/") and homepage:
        return homepage.rstrip("/") + value
    if "." in value and " " not in value and not value.startswith("#"):
        return "https://" + value
    return ""


def _row_key(row) -> str:
    if isinstance(row, dict):
        return str(row.get("page") or row.get("url") or row.get("0") or "")
    if isinstance(row, (list, tuple)):
        return str(row[0] if row else "")
    return str(row or "")


def site_limit_for(user: User) -> int:
    n = int(getattr(user, "site_limit", 0) or 0)
    if n > 0:
        return max(1, min(n, 100))
    if user.role == "ROLE_ADMIN":
        return PLAN_LIMITS["scale"]
    return PLAN_LIMITS.get((getattr(user, "plan", None) or "starter").lower(), 5)


def workspace_plan(user: User, used: int) -> dict:
    limit = site_limit_for(user)
    return {
        "plan": getattr(user, "plan", None) or "starter",
        "limit": limit,
        "used": used,
        "canSetLimit": user.role == "ROLE_ADMIN",
    }


class PlanBody(BaseModel):
    plan: str | None = None
    site_limit: int | None = None
    user_id: int | None = None


class CmsConnectBody(BaseModel):
    provider: str = Field(pattern="^(wordpress|shopify|webflow|custom)$")
    label: str = ""
    site_url: str = ""
    credentials: dict = Field(default_factory=dict)


class ChangeBody(BaseModel):
    opportunity: str
    target_url: str = ""
    change_type: str = "content"
    source: str = "manual"
    cms_connection_id: int | None = None
    draft_with_ai: bool = True
    remote_id: str = ""
    resource: str = "post"  # post|page for WP


class ExecuteBody(BaseModel):
    cms_connection_id: int | None = None
    force_dry_run: bool = False


class FromDraftBody(BaseModel):
    draft_id: int
    target_url: str = ""
    change_type: str = "content"
    cms_connection_id: int | None = None
    auto_approve: bool = True
    execute: bool = False
    force_dry_run: bool = True


class PatchChangeBody(BaseModel):
    title: str | None = None
    metaDescription: str | None = None
    opportunity: str | None = None
    remote_id: str | None = None
    resource: str | None = None


class GenerateMetaBody(BaseModel):
    tone: str = "Clear & direct"
    breadth: str = "balanced"


class FromGscBody(BaseModel):
    breadth: str = "balanced"
    site: str = ""
    brief: dict = Field(default_factory=dict)
    limit: int = 8
    rescan: bool = False


class BusinessProfileBody(BaseModel):
    business: str = ""
    services: str = ""
    areas: str = ""
    locations: str = ""
    claims: str = ""
    voice: str = ""
    restrictions: str = ""
    rules: str = ""


class AssistantTurn(BaseModel):
    role: str = "user"
    text: str = ""


class AssistantBody(BaseModel):
    messages: list[AssistantTurn] = Field(default_factory=list)
    queue: list[dict] = Field(default_factory=list)
    breadth: str = ""
    mode: str = ""


class VisibilityQueueBody(BaseModel):
    checkId: str = ""
    targetUrl: str = ""
    title: str = ""
    description: str = ""
    opportunity: str = ""


def _mask_creds(creds: dict) -> dict:
    out = {}
    for k, v in (creds or {}).items():
        if k.lower() in {"password", "applicationpassword", "accesstoken", "secret", "token", "apikey"}:
            s = str(v or "")
            out[k] = ("••••" + s[-4:]) if len(s) > 4 else "••••"
        else:
            out[k] = v
    return out


def _conn_row(row: CmsConnection) -> dict:
    return {
        "id": row.id,
        "provider": row.provider,
        "label": row.label or row.provider.title(),
        "siteUrl": row.site_url,
        "status": row.status,
        "credentialsMasked": _mask_creds(row.credentials or {}),
        "meta": row.meta or {},
        "lastError": row.last_error or "",
        "lastUsedAt": row.last_used_at.isoformat() if row.last_used_at else None,
        "createdAt": row.created_at.isoformat(),
    }


def _change_row(row: SiteChange) -> dict:
    return {
        "id": row.id,
        "cmsConnectionId": row.cms_connection_id,
        "draftId": row.draft_id,
        "source": row.source,
        "opportunity": row.opportunity,
        "targetUrl": row.target_url,
        "changeType": row.change_type,
        "proposed": row.proposed or {},
        "status": row.status,
        "execution": row.execution or {},
        "monitoring": row.monitoring or {},
        "createdAt": row.created_at.isoformat(),
        "updatedAt": row.updated_at.isoformat(),
        "appliedAt": row.applied_at.isoformat() if row.applied_at else None,
        "approvedBy": getattr(row, "approved_by", None),
        "approvedByName": getattr(row, "approved_by_name", "") or "",
        "approvedAt": row.approved_at.isoformat() if getattr(row, "approved_at", None) else None,
    }


def _upsert_feature(db: Session, user_id: int, kind: str, title: str, payload: dict):
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


def _sync_operator_features(db: Session, user: User):
    conns = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).order_by(CmsConnection.id.desc()).all()
    changes = db.query(SiteChange).filter(SiteChange.customer_id == user.id).order_by(SiteChange.id.desc()).limit(50).all()

    _upsert_feature(
        db,
        user.id,
        "cms-connectors",
        "CMS Connectors",
        {
            "summary": "WordPress · Shopify · Webflow · custom website connectors for approved SEO changes.",
            "columns": ["Provider", "Label", "Site", "Status"],
            "rows": [[c.provider, c.label or c.provider, c.site_url or "—", c.status] for c in conns],
            "kpis": [["Connectors", str(len(conns))], ["Live-ready", str(sum(1 for c in conns if c.status == "connected"))]],
            "source": "searchify_operator",
            "seedVersion": LIVE,
        },
    )

    change_rows = [
        [
            str(c.id),
            c.status,
            (c.opportunity or "")[:60],
            c.target_url[-40:] if c.target_url else "—",
            (c.execution or {}).get("provider") or "—",
            "dry-run" if (c.execution or {}).get("dryRun") else ("live" if c.applied_at else "—"),
        ]
        for c in changes
    ]
    _upsert_feature(
        db,
        user.id,
        "change-history",
        "Site Change History",
        {
            "summary": "Recorded SEO actions: proposed → approved → applied on CMS → monitored.",
            "columns": ["ID", "Status", "Opportunity", "URL", "CMS", "Mode"],
            "rows": change_rows,
            "kpis": [
                ["Changes", str(len(changes))],
                ["Applied", str(sum(1 for c in changes if c.status in {"applied", "monitoring", "closed"}))],
                ["Awaiting approval", str(sum(1 for c in changes if c.status in {"proposed", "awaiting_approval"}))],
            ],
            "source": "searchify_operator",
            "seedVersion": LIVE,
        },
    )

    mon = [c for c in changes if c.status in {"applied", "monitoring", "closed"}]
    mon_rows = [
        [
            str(c.id),
            c.target_url[-50:] if c.target_url else "—",
            (c.monitoring or {}).get("baselinePosition") or "—",
            (c.monitoring or {}).get("latestPosition") or "pending",
            (c.monitoring or {}).get("note") or "Watching GSC after apply",
        ]
        for c in mon
    ]
    _upsert_feature(
        db,
        user.id,
        "outcome-monitor",
        "Outcome Monitor",
        {
            "summary": "After Searchify applies a change on the CMS, outcomes are tracked against owned GSC signals.",
            "columns": ["Change", "URL", "Baseline pos", "Latest pos", "Note"],
            "rows": mon_rows,
            "source": "searchify_operator",
            "seedVersion": LIVE,
        },
    )

    pipeline = [
        ["1. Sense", "GSC / crawl / AI", "Live Google + on-page facts"],
        ["2. Decide", "Searchify + Astra", "Opportunity + proposed change"],
        ["3. Approve", "Human (JEV gate)", "Auto-safe or inbox approve"],
        ["4. Execute", "WP / Shopify / Webflow / custom", "Connector applies on the real site"],
        ["5. Record", "Postgres history", "Immutable change log"],
        ["6. Monitor", "GSC re-check", "Did the metric move?"],
    ]
    _upsert_feature(
        db,
        user.id,
        "seo-operator",
        "SEO Operator",
        {
            "summary": "Searchify turns SEO opportunities into approved, executed, monitored website actions — not just reports.",
            "columns": ["Step", "Who", "What"],
            "rows": pipeline,
            "kpis": [
                ["CMS linked", str(len(conns))],
                ["Open changes", str(sum(1 for c in changes if c.status not in {"closed", "failed"}))],
                ["Applied", str(sum(1 for c in changes if c.applied_at))],
            ],
            "panels": {
                "kpis": [
                    ["CMS", str(len(conns))],
                    ["Queue", str(sum(1 for c in changes if c.status in {"proposed", "awaiting_approval", "approved"}))],
                    ["Applied", str(sum(1 for c in changes if c.applied_at))],
                ],
            },
            "usp": (
                "GSC → opportunity → AI prepares change → human approves → "
                "CMS execute (WordPress/Shopify/Webflow/custom) → record → monitor."
            ),
            "source": "searchify_operator",
            "seedVersion": LIVE,
            "concept": "operator",
        },
    )


@router.get("/connections")
def list_connections(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).order_by(CmsConnection.id.desc()).all()
    return {
        "providers": list(cms_connectors.PROVIDERS),
        "connections": [_conn_row(r) for r in rows],
        **workspace_plan(user, len(rows)),
    }


@router.post("/workspace/plan")
def set_workspace_plan(body: PlanBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "ROLE_ADMIN":
        raise HTTPException(status_code=403, detail="Only an admin can change website limits")
    target = db.get(User, body.user_id) if body.user_id else user
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    plan = (body.plan or target.plan or "starter").strip().lower()
    if plan not in PLAN_LIMITS and body.site_limit is None:
        raise HTTPException(status_code=400, detail="Use starter (5), agency (10), scale (15), or a custom site_limit")
    if plan in PLAN_LIMITS:
        target.plan = plan
        target.site_limit = body.site_limit or PLAN_LIMITS[plan]
    else:
        target.plan = "custom"
        target.site_limit = max(1, min(int(body.site_limit or target.site_limit or 5), 100))
    db.add(target)
    db.commit()
    db.refresh(target)
    used = db.query(CmsConnection).filter(CmsConnection.customer_id == target.id).count()
    return {"ok": True, **workspace_plan(target, used)}


HOT_FEATURE_KINDS = (
    "organic-search",
    "traffic-analytics",
    "top-pages",
    "site-audit",
    "local-seo",
    "keyword-research",
    "google-ads",
)


@router.get("/workspace")
def workspace_bootstrap(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """One round-trip for shell + first screens: Google, CMS, queue, hot features."""
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    cms = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).order_by(CmsConnection.id.desc()).all()
    stale = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id)
        .all()
    )
    for rec in stale:
        payload = rec.payload or {}
        seed = str(payload.get("seedVersion") or "")
        source = str(payload.get("source") or "")
        if seed != LIVE and not source.startswith("google") and source != "searchify_operator":
            db.delete(rec)
    if not (google and (google.access_token or google.refresh_token)):
        _clear_user_queue(db, user.id)
    if stale or not (google and (google.access_token or google.refresh_token)):
        db.commit()
    changes = (
        db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id)
        .order_by(SiteChange.id.desc())
        .limit(40)
        .all()
    )

    features: dict[str, dict] = {}
    recs = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind.in_(HOT_FEATURE_KINDS))
        .order_by(FeatureRecord.id.desc())
        .limit(80)
        .all()
    )
    recs = [
        r
        for r in recs
        if (r.payload or {}).get("seedVersion") == LIVE or str((r.payload or {}).get("source") or "").startswith("google")
    ]
    recs.sort(key=lambda r: -r.id)
    for row in recs:
        if row.kind in features:
            continue
        features[row.kind] = {
            "id": row.id,
            "kind": row.kind,
            "title": row.title,
            "payload": row.payload or {},
            "status": row.status,
            "createdAt": row.created_at.isoformat(),
        }
    return {
        "google": {"configured": True, "connection": _connection_payload(google)},
        "cms": {
            "providers": list(cms_connectors.PROVIDERS),
            "connections": [_conn_row(r) for r in cms],
            **workspace_plan(user, len(cms)),
        },
        "changes": [_change_row(r) for r in changes],
        "features": features,
    }


@router.post("/connections")
def upsert_connection(body: CmsConnectBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    label = body.label.strip() or body.provider.title()
    row = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.provider == body.provider, CmsConnection.site_url == body.site_url)
        .first()
    )
    if row is None:
        used = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).count()
        limit = site_limit_for(user)
        if used >= limit:
            raise HTTPException(
                status_code=400,
                detail=f"Site limit reached ({limit}). Disconnect a site or raise the workspace plan.",
            )
        row = CmsConnection(customer_id=user.id, provider=body.provider)
        db.add(row)
    row.label = label
    row.site_url = body.site_url.strip()
    # merge credentials so empty fields don't wipe secrets
    merged = dict(row.credentials or {})
    for k, v in (body.credentials or {}).items():
        if v is None or v == "" or str(v).startswith("••••"):
            continue
        merged[k] = v
    if body.provider == "wordpress":
        password = str(merged.get("applicationPassword") or merged.get("password") or "").replace(" ", "")
        if password:
            merged["applicationPassword"] = password
        probe = cms_connectors.probe_wordpress(merged, row.site_url)
        if not probe.get("ok"):
            db.rollback()
            raise HTTPException(status_code=400, detail=probe.get("detail") or "WordPress could not be verified.")
    row.credentials = merged
    row.status = "connected"
    row.updated_at = datetime.utcnow()
    row.last_error = ""
    db.commit()
    db.refresh(row)
    _sync_operator_features(db, user)
    return {"ok": True, "connection": _conn_row(row)}


def _google_is_connected(db: Session, user_id: int) -> bool:
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user_id).first()
    return bool(row and (row.access_token or row.refresh_token) and row.status != "waiting_for_provider")


def _clear_user_queue(db: Session, user_id: int) -> int:
    rows = db.query(SiteChange).filter(SiteChange.customer_id == user_id).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def _purge_queue_without_google(db: Session, user: User) -> int:
    """Search Console drafts go when Google is gone. Drafts written from the site scan stay."""
    if _google_is_connected(db, user.id):
        return 0
    rows = (
        db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id, SiteChange.source == "gsc")
        .all()
    )
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(CmsConnection, connection_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Connection not found")
    for change in db.query(SiteChange).filter(SiteChange.cms_connection_id == row.id).all():
        db.delete(change)
    db.delete(row)
    remaining = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).count()
    if remaining == 0 and not _google_is_connected(db, user.id):
        _clear_user_queue(db, user.id)
    db.commit()
    _sync_operator_features(db, user)
    return {"ok": True}


@router.post("/workspace/reset")
def reset_workspace(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Force disconnect Google + WordPress and wipe cached live metrics / work queue.

    Use when switching sites (e.g. away from an old GSC property) so Performance and
    Work queue start empty until you reconnect and sync.
    """
    from app.models import GoogleConnection

    cleared = {
        "google": 0,
        "cms": 0,
        "changes": 0,
        "features": 0,
        "jobs": 0,
    }

    g = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if g:
        db.delete(g)
        cleared["google"] = 1

    cms_rows = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).all()
    cleared["cms"] = len(cms_rows)
    for row in cms_rows:
        db.delete(row)

    changes = db.query(SiteChange).filter(SiteChange.customer_id == user.id).all()
    cleared["changes"] = len(changes)
    for row in changes:
        db.delete(row)

    # Wipe live Google / Places / PSI / operator feature caches (keep unrelated drafts if any)
    feature_rows = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id)
        .all()
    )
    for fr in feature_rows:
        payload = fr.payload or {}
        source = str(payload.get("source") or "")
        seed = str(payload.get("seedVersion") or "")
        live = seed == LIVE or source.startswith("google") or source.startswith("places")
        operatorish = fr.kind in {
            "organic-search",
            "traffic-analytics",
            "traffic-home",
            "top-pages",
            "site-audit",
            "local-seo",
            "local-listings",
            "position-tracking",
            "gsc-countries",
            "gsc-devices",
            "organic-research",
            "website-keywords",
            "keyword-research",
            "ai-visibility",
            "business-profile",
            "cms-connectors",
            "seo-operator",
            "change-history",
            "opportunity-monitor",
        }
        if live or operatorish:
            db.delete(fr)
            cleared["features"] += 1

    jobs = db.query(Job).filter(Job.customer_id == user.id).all()
    cleared["jobs"] = len(jobs)
    for row in jobs:
        db.delete(row)

    db.commit()
    return {"ok": True, "cleared": cleared, "message": "Workspace reset. Reconnect WordPress + Google, then sync."}


@router.get("/changes")
def list_changes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _purge_queue_without_google(db, user)
    rows = db.query(SiteChange).filter(SiteChange.customer_id == user.id).order_by(SiteChange.id.desc()).limit(100).all()
    return [_change_row(r) for r in rows]


def _create_change(db: Session, user: User, body: ChangeBody) -> SiteChange:
    proposed: dict = {
        "title": "",
        "content": "",
        "metaDescription": "",
        "remoteId": body.remote_id,
        "resource": body.resource,
        "changeType": body.change_type,
        "targetUrl": body.target_url,
    }
    draft_id = None
    status = "awaiting_approval"

    if body.draft_with_ai and openai_configured():
        brief = (
            f"Opportunity: {body.opportunity}\n"
            f"Target URL: {body.target_url or '(site homepage / TBD)'}\n"
            f"Change type: {body.change_type}\n\n"
            "Write a concrete website change: improved title (≤60 chars), meta description (≤155), "
            "and a short content/HTML snippet ready for CMS. Do not invent traffic or ranks."
        )
        try:
            text, model, role = generate_draft_body("content-optimizer", brief, writing_type="On-site SEO fix", tokens=700)
            from app.jev import score_draft

            jev = score_draft("content-optimizer", brief, text)
            draft = Draft(
                customer_id=user.id,
                kind="site-change",
                brief=brief,
                body=text,
                status="approved" if jev.get("band") == "auto" else "waiting_for_writer",
                score=jev,
            )
            db.add(draft)
            db.flush()
            draft_id = draft.id
            proposed.update({"draftBody": text, "model": model, "role": role, "jev": jev})
            for line in text.splitlines():
                line = line.strip().lstrip("#").strip()
                if line and not proposed["title"] and len(line) < 90:
                    proposed["title"] = line[:70]
                    break
            proposed["content"] = text
            status = "approved" if jev.get("band") == "auto" else "awaiting_approval"
        except Exception as exc:  # noqa: BLE001
            proposed["aiError"] = str(exc)[:200]

    change = SiteChange(
        customer_id=user.id,
        cms_connection_id=body.cms_connection_id,
        draft_id=draft_id,
        source=body.source,
        opportunity=body.opportunity.strip(),
        target_url=body.target_url.strip(),
        change_type=body.change_type,
        proposed=proposed,
        status=status,
    )
    db.add(change)
    db.add(Job(customer_id=user.id, kind="site_change", status="queued", detail={"opportunity": body.opportunity[:120]}))
    db.commit()
    db.refresh(change)
    return change


@router.post("/changes")
def create_change(body: ChangeBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    change = _create_change(db, user, body)
    _sync_operator_features(db, user)
    return _change_row(change)


def _profile_for(db: Session, user_id: int) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "business-profile")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict((row.payload if row else {}) or {})


@router.get("/business-profile")
def get_business_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"profile": _profile_for(db, user.id)}


@router.put("/business-profile")
def put_business_profile(body: BusinessProfileBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    profile = {
        "business": body.business.strip(),
        "services": body.services.strip(),
        "areas": body.areas.strip(),
        "locations": (body.locations or body.areas or "").strip(),
        "claims": body.claims.strip(),
        "voice": body.voice.strip(),
        "restrictions": (body.restrictions or body.rules or "").strip(),
        "rules": (body.restrictions or body.rules or "").strip(),
    }
    _upsert_feature(db, user.id, "business-profile", "Business Profile", profile)
    return {"ok": True, "profile": profile}


def _clip(value, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _assistant_status(db: Session, user: User) -> list[str]:
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    sites = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .all()
    )
    open_count = (
        db.query(SiteChange)
        .filter(
            SiteChange.customer_id == user.id,
            SiteChange.status.in_(["proposed", "awaiting_approval"]),
        )
        .count()
    )
    wordpress = ", ".join(_clip(row.site_url, 80) for row in sites if row.site_url) or "not connected"
    gsc = _clip(getattr(google, "gsc_site_url", "") or "", 120) or "not selected"
    analytics = _clip(getattr(google, "ga4_property_name", "") or getattr(google, "ga4_property_id", "") or "", 80) or "not selected"
    return [
        f"WordPress: {wordpress}",
        f"Search Console: {gsc}",
        f"Analytics: {analytics}",
        f"Open suggestions stored: {open_count}",
    ]


def _assistant_fallback(question: str, profile: dict, queue: list[dict], status: list[str] | None = None) -> str:
    business = _clip(profile.get("business") or profile.get("services") or "the business in your setup", 120)
    first = queue[0] if queue else {}
    pending = [item for item in queue if str(item.get("decision") or "pending") == "pending"]
    target = pending[0] if pending else first
    lower = question.lower()
    joined = " ".join(status or [])
    if any(word in lower for word in ("cost", "price", "how much", "cheap")):
        return (
            "Writing the title and description is the small part. One page is under a cent. "
            "Five pages is about 3 cents, and ten pages is about 6 cents. "
            "If a draft has to be rewritten, that page costs about twice. "
            "That figure is the writing step only. Nothing is published from this chat. "
            "If you want, I can walk you through the next card."
        )
    if any(word in lower for word in ("next", "how do", "how does", "start", "help", "what should", "where")):
        if "not selected" in joined and "Search Console: not selected" in joined:
            return (
                f"For {business}, the useful next step is Connections: choose the Search Console property and sync. "
                "Then press Write title suggestions. Each card shows the current title and the suggested one. "
                "Approve the ones you want. This chat cannot publish them."
            )
        if pending:
            return (
                f"You already have {len(pending)} suggestion{'s' if len(pending) != 1 else ''} to review for {business}. "
                f"I’d open “{_clip(target.get('title') or 'the first card', 80)}” first. "
                "Dismiss it if it is weak, or approve it on the card. Writing another batch skips pages that are already in the queue."
            )
        return (
            f"The queue for {business} is clear. Press Write title suggestions to read the connected pages and draft a title and description for each. "
            "A 5 to 10 page site is a small writing job. You still approve every card before anything goes live."
        )
    if target and any(word in lower for word in ("why", "first", "this", "card", "suggest", "title", "description")):
        live = "It comes from the connected page data." if target.get("live") else "It comes from the setup brief, not a live Search Console page yet."
        return (
            f"The one I’d look at first is {_clip(target.get('title') or 'the open suggestion', 90)} "
            f"on {_clip(target.get('url') or 'that page', 80)}. "
            f"The current title is “{_clip(target.get('before') or 'not stored', 70)}” and the suggestion is "
            f"“{_clip(target.get('after') or 'not written yet', 70)}”. {live} "
            "If it does not sound like the page, dismiss it and write again."
        )
    if any(word in lower for word in ("approve", "publish", "live")):
        return (
            "Approving a card does not publish it from this chat. "
            "Use Approve draft on the card. A live recommendation still waits for the publishing step. "
            "Cards that only come from the setup brief stay here until Search Console is connected."
        )
    if any(word in lower for word in ("contact", "admin", "support", "person")):
        return (
            "I can point you to the contact page so you can write to the Searchify team. "
            "Mention the website and what you were trying to do. I don’t send the message myself."
        )
    if "wordpress" in lower:
        return (
            "WordPress is how an approved title and description can be published. "
            "Open Connections and connect that site. Connecting does not publish the page, and I can’t enter the password for you."
        )
    if any(word in lower for word in ("google", "analytics", "search console")):
        return (
            "Search Console shows queries people already use, and Analytics shows which pages they open. "
            "Open Connections, continue with Google, then choose this website’s property. "
            "If the site uses a different Google login, add that login in Manage workspace first. I can’t sign in for you."
        )
    if "keyword" in lower:
        return (
            "Keywords start from what you sell and where you serve. "
            "After Search Console is connected, the Keywords page can list queries Google already recorded. "
            "Searchify does not invent a rank or a search volume."
        )
    if "backlink" in lower or "referring" in lower:
        return (
            "Backlinks are referring domains stored for the selected website. "
            "An empty list means none are stored yet. A lost link is a cue to review it, not an automatic disavow."
        )
    if "visibility" in lower:
        return (
            "AI visibility is a short list of questions a customer might ask about the business. "
            "Run live checks to ask ChatGPT, Gemini, and Perplexity and see whether the business is named or the site is cited, "
            "with the full answer and its sources."
        )
    if any(word in lower for word in ("plan", "subscription", "billing", "usage", "package")):
        return (
            "Subscription shows the plan: websites, keywords, prompts, and audits. "
            "The usage line is what this workspace has already used. Change the plan there when you need another site."
        )
    count = len(pending) or len(queue)
    if count:
        return (
            f"There are {count} open item{'s' if count != 1 else ''} for {business}. "
            "Want me to start with the first one, or talk through what approve actually does?"
        )
    return (
        f"I have the brief for {business}, and this queue is still empty. "
        "Connect Search Console, sync, then press Write title suggestions. I’ll stay with you while you review them."
    )


@router.post("/assistant")
def ask_assistant(body: AssistantBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Conversational help for the review queue. Does not approve or publish."""
    profile = _profile_for(db, user.id)
    turns = [turn for turn in body.messages if _clip(turn.text, 800)][-8:]
    if not turns:
        raise HTTPException(status_code=400, detail="Ask a question about this queue.")
    question = _clip(turns[-1].text, 800)
    queue = []
    for item in (body.queue or [])[:8]:
        if not isinstance(item, dict):
            continue
        queue.append(
            {
                "title": _clip(item.get("title"), 140),
                "url": _clip(item.get("url"), 180),
                "before": _clip(item.get("before"), 140),
                "after": _clip(item.get("after"), 140),
                "description": _clip(item.get("afterDescription") or item.get("description"), 220),
                "kind": _clip(item.get("kind"), 40),
                "decision": _clip(item.get("decision") or "pending", 20),
                "live": bool(item.get("live")),
                "reason": _clip(item.get("reason"), 180),
                "sources": [_clip(source, 60) for source in (item.get("sources") or [])[:4]],
            }
        )
    status = _assistant_status(db, user)
    if not openai_configured():
        return {"reply": _assistant_fallback(question, profile, queue, status), "model": "", "source": "brief"}

    lines = [
        f"Business: {_clip(profile.get('business'), 200) or 'not set'}",
        f"Services: {_clip(profile.get('services'), 200) or 'not set'}",
        f"Market: {_clip(profile.get('areas') or profile.get('locations'), 200) or 'not set'}",
        f"Voice: {_clip(profile.get('voice'), 160) or 'not set'}",
        f"Do not claim: {_clip(profile.get('restrictions') or profile.get('rules'), 200) or 'none'}",
        f"Working style: {_clip(body.breadth, 80) or 'not set'}",
        f"Approval policy: {_clip(body.mode, 40) or 'review'}",
        "Workspace:",
        *status,
        "Queue on screen:",
    ]
    if queue:
        for index, item in enumerate(queue, start=1):
            origin = "live page data" if item["live"] else "setup brief only"
            lines.append(
                f"{index}. [{item['decision']}] {item['title']} — {item['url']} ({origin}). "
                f"Current: {item['before']}. Suggested: {item['after']}. "
                f"Description: {item['description'] or 'none'}. Why: {item.get('reason') or 'not stored'}. "
                f"Sources: {', '.join(item['sources']) or 'brief'}."
            )
    else:
        lines.append("No cards are on screen.")
    lines.append("Conversation so far:")
    for turn in turns:
        role = "Searchify" if turn.role == "assistant" else "Operator"
        lines.append(f"{role}: {_clip(turn.text, 500)}")
    system = (
        "You are Searchify assistance, in a real conversation with the person using this workspace. "
        "Sound friendly, calm, and easy to talk to. Use plain words. Keep the reply smooth, like a colleague sitting beside them. "
        "If asked who you are, say you are Searchify assistance. "
        "Never name a model, GPT, OpenAI, Luna, Terra, Sol, or 4o. "
        "Answer the question they asked first. Then offer one helpful suggestion tied to that question: a next step in the product, or a follow-up they might want to ask. One suggestion is enough. "
        "Usually three to six sentences. Use a short numbered list only when they ask what to do next. No headings. "
        "The working style in the message changes the wording, not publishing. Exact-match stays on the phrase already on the page. Balanced uses the closest honest query. Broader discovery may use one nearby angle the page already supports. "
        "Review every change means they press Generate, then approve or dismiss each card. Prepare drafts automatically fills empty cards when they open the overview. Neither choice publishes. "
        "Help them use the system: connect WordPress, choose Search Console and Analytics, sync, press Generate, then dismiss or approve each card. "
        "Notice what they are trying to finish, name why that step helps in one plain sentence, then say how to do it. "
        "You cannot sign in to Google or WordPress for them, and you cannot publish. Offer the page: Connections for Google and WordPress, Manage workspace to add a website or another Google login, Keywords for live Google positions, volume, and ideas, Backlinks for the live link index, AI visibility for live checks of whether ChatGPT, Gemini, and Perplexity name or cite the business, Subscription for the plan and usage, and the contact page if they want a person on the team. "
        "A lost backlink is a review cue, not a disavow. Do not invent ranks, volumes, scores, or link counts. "
        "Each website can use its own Google account. Overview and Settings follow the selected website. Keywords, Backlinks, AI visibility, and the completion log can stay on one website for that page only. "
        "A title suggestion reads the live page, competitor listings, Search Console, and Analytics. The output is one title and one description. It does not publish. "
        "If they ask cost, say this and do not invent another price: one page is under a cent, five pages about 3 cents, ten pages about 6 cents. "
        "A rewritten page costs about twice. That is the writing step only. "
        "Use the workspace status and the queue in the message. Name the page when you refer to a card. "
        "If a fact is not there, say so in a friendly way and suggest what would unlock it. Do not invent rankings, clicks, traffic, or backlinks. "
        "Do not say you approved or published anything. Approval happens on the card, not in this chat. "
        "If a card is marked setup brief only, say that it is not from Search Console yet. "
        "When the queue already has those pages, suggest dismissing a card before writing that page again."
    )
    try:
        text, model, _role = chat_text(
            system=system,
            user="\n".join(lines),
            kind="assistant",
            brief=question,
            max_tokens=700,
        )
    except RuntimeError:
        return {"reply": _assistant_fallback(question, profile, queue, status), "model": "", "source": "brief"}
    return {"reply": text.strip(), "model": model, "source": "model"}


def _analytics_for_page(db: Session, user_id: int, url: str) -> dict:
    """Compact GA4 evidence for one page. Empty when Analytics was never synced."""
    from urllib.parse import urlparse

    def latest(kind: str) -> dict:
        rec = (
            db.query(FeatureRecord)
            .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
            .order_by(FeatureRecord.id.desc())
            .first()
        )
        if rec is None:
            return {}
        payload = rec.payload or {}
        source = str(payload.get("source") or "")
        if payload.get("seedVersion") != LIVE and not source.startswith("google"):
            return {}
        return payload

    traffic = latest("traffic-analytics")
    channels = latest("traffic-distribution")
    landings = latest("ga4-landing-pages")
    path = (urlparse(url).path or "/").rstrip("/") or "/"
    matched = None
    for row in landings.get("rows") or []:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        landing = str(row[0] or "").rstrip("/") or "/"
        if path == "/" and landing in {"/", ""}:
            matched = row
            break
        if path != "/" and path in landing:
            matched = row
            break
    connected = bool(traffic.get("kpis") or channels.get("rows") or landings.get("rows"))
    return {
        "connected": connected,
        "kpis": list(traffic.get("kpis") or [])[:4],
        "channels": list(channels.get("rows") or [])[:6],
        "landing": list(matched) if matched else None,
    }


SCAN_MAX_AGE_HOURS = 72
ROLE_ORDER = {"home": 0, "service": 1, "product": 1, "category": 2, "location": 2, "other": 3, "about": 4, "contact": 5, "blog": 6}


def _brief_profile(profile: dict, brief: dict | None) -> dict:
    data = dict(profile or {})
    b = brief or {}
    if not data.get("services") and b.get("businessType"):
        data["services"] = b["businessType"]
    if not (data.get("areas") or data.get("locations")) and b.get("market"):
        data["areas"] = data["locations"] = b["market"]
    if b.get("goal"):
        data["goal"] = b["goal"]
    if b.get("avoid"):
        rules = data.get("restrictions") or data.get("rules") or ""
        data["restrictions"] = f"{rules} Never suggest: {b['avoid']}".strip()
    if b.get("sensitive"):
        data["restrictions"] = f"{data.get('restrictions') or ''} Sensitive category: {b['sensitive']}, keep claims careful.".strip()
    return data


def _scan_title(host: str) -> str:
    return f"Site scan {host}"


def _latest_scan(db: Session, user_id: int, host: str) -> dict:
    if not host:
        return {}
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "site-scan", FeatureRecord.title == _scan_title(host))
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict((row.payload if row else {}) or {})


def _scan_fresh(scan: dict) -> bool:
    stamp = scan.get("scannedAt")
    if not stamp or not scan.get("pages"):
        return False
    try:
        age = datetime.utcnow() - datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return age.total_seconds() < SCAN_MAX_AGE_HOURS * 3600


def _places_rows(db: Session, user_id: int) -> list:
    rec = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "local-competitors")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return list(((rec.payload if rec else {}) or {}).get("rows") or [])


def _ensure_scan(db: Session, user: User, home: str, brief: dict | None, force: bool = False) -> tuple[dict, bool]:
    from app import site_scan

    host = site_scan.host_of(home)
    scan = _latest_scan(db, user.id, host)
    if scan and _scan_fresh(scan) and not force:
        return scan, False
    profile = _brief_profile(_profile_for(db, user.id), brief)
    scan = site_scan.scan_site(home, brief=brief or {}, profile=profile, places_rows=_places_rows(db, user.id))
    if scan.get("pages"):
        _upsert_feature(db, user.id, "site-scan", _scan_title(host), scan)
    return scan, True


def _scan_summary(scan: dict, open_urls: set[str] | None = None) -> dict:
    pages = scan.get("pages") or []
    business = scan.get("business") or {}
    rivals = scan.get("competitors") or {}
    sources = sorted({r.get("source") or "competitor" for rows in rivals.values() for r in rows})
    page_map = scan.get("pageMap") or {}
    draftable = [p for p in pages if (page_map.get(p["url"]) or {}).get("role") != "legal" and not p.get("noindex")]
    covered = len([p for p in draftable if p["url"].rstrip("/") in (open_urls or set())])
    return {
        "host": scan.get("host") or "",
        "scannedAt": scan.get("scannedAt") or "",
        "pagesRead": len(pages),
        "draftable": len(draftable),
        "covered": covered,
        "business": business.get("business") or "",
        "brand": business.get("brand") or "",
        "offers": (business.get("offers") or [])[:8],
        "places": (business.get("places") or [])[:6],
        "proof": (business.get("proof") or [])[:6],
        "competitorSearches": len(rivals),
        "competitorPages": sum(len(rows) for rows in rivals.values()),
        "competitorSources": sources,
        "error": scan.get("error") or "",
    }


def _open_urls(db: Session, user_id: int) -> set[str]:
    return {
        (c.target_url or "").rstrip("/")
        for c in db.query(SiteChange)
        .filter(
            SiteChange.customer_id == user_id,
            SiteChange.status.in_(["proposed", "awaiting_approval", "approved", "dismissed"]),
        )
        .all()
    }


class SiteScanBody(BaseModel):
    site: str = ""
    brief: dict = Field(default_factory=dict)
    force: bool = False


def _home_for(db: Session, user: User, site: str = "") -> str:
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    cms_rows = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .all()
    )
    return _homepage_url(site, *(c.site_url for c in cms_rows), google.gsc_site_url if google else "")


@router.post("/site-scan")
def run_site_scan(body: SiteScanBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    home = _home_for(db, user, body.site)
    if not home:
        raise HTTPException(status_code=400, detail="Add the website address in setup or connect WordPress first.")
    scan, ran = _ensure_scan(db, user, home, body.brief, force=body.force)
    if not scan.get("pages"):
        raise HTTPException(status_code=422, detail=scan.get("error") or "No readable pages were found on this website.")
    return {"ok": True, "ran": ran, "scan": _scan_summary(scan, _open_urls(db, user.id))}


@router.get("/site-scan")
def get_site_scan(site: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app import site_scan

    home = _home_for(db, user, site)
    scan = _latest_scan(db, user.id, site_scan.host_of(home)) if home else {}
    if not scan:
        return {"ok": True, "scan": None}
    return {"ok": True, "scan": _scan_summary(scan, _open_urls(db, user.id))}


def _site_context(scan: dict, url: str) -> tuple[dict | None, dict, list[dict]]:
    """(scanned page, site context for the writer, competitor pages for this page's search)."""
    from app.site_scan import normalize

    if not scan:
        return None, {}, []
    key = normalize(url)
    pages = {p["url"]: p for p in scan.get("pages") or []}
    page_map = scan.get("pageMap") or {}
    info = page_map.get(key) or page_map.get(key.rstrip("/")) or {}
    siblings = [
        {"url": p["url"], "title": p.get("title") or "", "target": (page_map.get(p["url"]) or {}).get("target") or ""}
        for p in (scan.get("pages") or [])
        if p["url"] != key and (page_map.get(p["url"]) or {}).get("role") != "legal"
    ]
    rivals = (scan.get("competitors") or {}).get(info.get("target") or "") or []
    if not rivals:
        for rows in (scan.get("competitors") or {}).values():
            rivals = rows
            break
    return pages.get(key), {"business": scan.get("business") or {}, "page": info, "siblings": siblings}, rivals


@router.post("/changes/from-gsc")
def propose_from_gsc(
    body: FromGscBody | None = Body(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Read the whole site, understand the business and competitors, then draft a title and description per page."""
    from concurrent.futures import ThreadPoolExecutor

    from app.meta_writer import _overlap, write_title_description
    from app.site_scan import normalize

    body = body or FromGscBody()
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    has_gsc = bool(google and (google.gsc_site_url or "").strip())
    cms_rows = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .all()
    )

    def latest(kind: str) -> FeatureRecord | None:
        return (
            db.query(FeatureRecord)
            .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == kind)
            .order_by(FeatureRecord.id.desc())
            .first()
        )

    def is_live(rec: FeatureRecord | None) -> bool:
        if rec is None:
            return False
        payload = rec.payload or {}
        return payload.get("seedVersion") == LIVE or str(payload.get("source") or "").startswith("google")

    pages_rec, organic_rec = latest("top-pages"), latest("organic-search")
    homepage = _homepage_url(
        body.site,
        *(c.site_url for c in cms_rows),
        google.gsc_site_url if google else "",
        (pages_rec.payload or {}).get("siteUrl") if pages_rec else "",
    )
    if not homepage:
        raise HTTPException(
            status_code=400,
            detail="Add the website address in setup, or connect WordPress or Search Console, then try again.",
        )

    page_rows: list = list((pages_rec.payload or {}).get("rows") or []) if is_live(pages_rec) else []
    queries: list = list((organic_rec.payload or {}).get("rows") or []) if is_live(organic_rec) else []
    if has_gsc and not page_rows:
        try:
            from app import google_oauth as goauth
            from app.routers.google_connect import _valid_access_token

            token = _valid_access_token(db, google)
            page_rows = goauth.fetch_gsc_top_pages(token, google.gsc_site_url, days=28, row_limit=25)
            queries = queries or goauth.fetch_gsc_top_queries(token, google.gsc_site_url, days=28, row_limit=25)
            db.add(FeatureRecord(customer_id=user.id, kind="top-pages", title="Top Pages", status="stored",
                                 payload={"rows": page_rows, "source": "google_search_console", "seedVersion": LIVE}))
            db.add(FeatureRecord(customer_id=user.id, kind="organic-search", title="Organic Search", status="stored",
                                 payload={"rows": queries, "source": "google_search_console", "seedVersion": LIVE}))
            db.flush()
        except Exception:  # noqa: BLE001
            page_rows = []

    evidence: dict[str, dict] = {}
    for row in page_rows:
        url = _page_url(_row_key(row), homepage)
        if not url:
            continue
        cells = list(row) if isinstance(row, (list, tuple)) else []
        evidence[normalize(url)] = {
            "clicks": cells[1] if len(cells) > 1 else "—",
            "impressions": cells[2] if len(cells) > 2 else "—",
            "position": cells[4] if len(cells) > 4 else "—",
        }

    scan, scanned_now = _ensure_scan(db, user, homepage, body.brief, force=body.rescan)
    profile = _brief_profile(_profile_for(db, user.id), body.brief)
    page_map = scan.get("pageMap") or {}

    query_terms = [str(q[0]).strip() for q in queries if isinstance(q, (list, tuple)) and q and str(q[0]).strip()][:8]
    services, areas = profile.get("services") or "", profile.get("areas") or profile.get("locations") or ""
    if not query_terms and (services or areas):
        query_terms = [" ".join(part for part in (services.split(",")[0].strip(), areas.split(",")[0].strip()) if part)]

    fallback_rivals: list[dict] = []
    if not scan.get("competitors"):
        try:
            from app import dataforseo

            seed = query_terms[0] if query_terms else f"{services.split(',')[0]} {areas}".strip()
            fallback_rivals = dataforseo.competitor_serp(seed)
        except Exception:  # noqa: BLE001
            fallback_rivals = []

    def rank(url: str) -> tuple:
        info = page_map.get(url) or {}
        ev = evidence.get(url) or {}
        try:
            impressions = -float(str(ev.get("impressions") or 0).replace(",", ""))
        except ValueError:
            impressions = 0.0
        return (0 if url in evidence else 1, impressions, ROLE_ORDER.get(info.get("role") or "other", 3), len(url))

    scanned_urls = [
        p["url"] for p in scan.get("pages") or []
        if not p.get("noindex") and (page_map.get(p["url"]) or {}).get("role") != "legal"
    ]
    candidates = sorted(dict.fromkeys([*evidence.keys(), *scanned_urls]), key=rank)
    if not candidates:
        candidates = [normalize(homepage)]

    open_urls = _open_urls(db, user.id)
    pending = [url for url in candidates if url.rstrip("/") not in open_urls]
    limit = max(1, min(int(body.limit or 8), 12))
    if scanned_now:
        limit = min(limit, 5)
    batch, remaining = pending[:limit], max(0, len(pending) - limit)

    gsc_url = (google.gsc_site_url or "").strip() if google else ""
    cms_for_site = next((c for c in cms_rows if gsc_url and (c.meta or {}).get("gscSiteUrl") == gsc_url), None)
    if cms_for_site is None:
        from app.site_scan import host_of

        cms_for_site = next((c for c in cms_rows if host_of(c.site_url or "") == host_of(homepage)), None)
    if cms_for_site is None and len(cms_rows) == 1:
        cms_for_site = cms_rows[0]

    jobs = []
    for url in batch:
        page, site_ctx, rivals = _site_context(scan, url)
        jobs.append({
            "url": url,
            "page": page,
            "site": site_ctx,
            "rivals": rivals or fallback_rivals,
            "gsc": evidence.get(url) or {"clicks": "—", "impressions": "—", "position": "—"},
            "analytics": _analytics_for_page(db, user.id, url),
        })

    def draft(job: dict, taken: list[str] | None = None) -> dict:
        try:
            return write_title_description(
                url=job["url"],
                profile=profile,
                gsc=job["gsc"],
                queries=query_terms,
                competitors=job["rivals"],
                analytics=job["analytics"],
                specificity=body.breadth or "balanced",
                page=job["page"],
                site=job["site"],
                taken=taken,
            )
        except Exception as exc:  # noqa: BLE001
            return {"aiError": str(exc)[:200]}

    results: list[dict] = []
    if openai_configured() and jobs:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(draft, jobs))
        rewrites = 0
        for index, result in enumerate(results):
            title = result.get("title") or ""
            earlier = [r.get("title") or "" for r in results[:index]]
            if title and rewrites < 2 and any(_overlap(title, other) >= 0.6 for other in earlier if other):
                results[index] = draft(jobs[index], taken=[t for t in earlier if t])
                rewrites += 1
    else:
        results = [{} for _ in jobs]

    created = []
    for job, drafted in zip(jobs, results):
        url = job["url"]
        scraped = drafted.get("scraped") or job["page"] or {}
        info = job["site"].get("page") or {}
        change = SiteChange(
            customer_id=user.id,
            cms_connection_id=cms_for_site.id if cms_for_site else None,
            source="gsc" if url in evidence else "site_scan",
            opportunity=f"Stronger Google title and description for {url}",
            target_url=url,
            change_type="meta",
            proposed={
                "title": drafted.get("title") or "",
                "metaDescription": drafted.get("metaDescription") or "",
                "reason": drafted.get("reason") or "",
                "target": drafted.get("target") or info.get("target") or "",
                "pageRole": info.get("role") or "",
                "beforeTitle": scraped.get("title") or "",
                "beforeDescription": scraped.get("description") or "",
                "pageH1": scraped.get("h1") or "",
                "evidence": job["gsc"],
                "changeType": "meta",
                "targetUrl": url,
                "draftBody": drafted.get("draftBody") or "",
                "model": drafted.get("model") or "",
                "role": drafted.get("role") or "",
                "competitors": [
                    {k: r.get(k) for k in ("title", "url", "description", "source")} for r in (job["rivals"] or [])[:6]
                ],
                "analytics": bool((job["analytics"] or {}).get("connected")),
                "specificity": drafted.get("specificity") or body.breadth or "balanced",
                "writer": "site-scan+brief+competitors+gsc+analytics",
                "profileHints": {k: profile.get(k) or "" for k in ("business", "services", "areas", "goal", "restrictions")},
                **({"aiError": drafted["aiError"]} if drafted.get("aiError") else {}),
                **({"scrapeError": scraped["error"]} if scraped.get("error") else {}),
            },
            status="awaiting_approval",
        )
        db.add(change)
        db.flush()
        created.append(_change_row(change))
        open_urls.add(url.rstrip("/"))

    db.commit()
    if created:
        _sync_operator_features(db, user)
    summary = _scan_summary(scan, open_urls) if scan else None
    if not created:
        return {
            "ok": True,
            "created": 0,
            "changes": [],
            "remaining": 0,
            "scan": summary,
            "reason": "Every page that was read already has a draft. Review or dismiss them to write new ones.",
        }
    return {"ok": True, "created": len(created), "changes": created, "remaining": remaining, "scan": summary}


@router.post("/changes/from-draft")
def change_from_draft(body: FromDraftBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Turn an approved (or approvable) AI draft into a SiteChange for CMS execute."""
    draft = db.get(Draft, body.draft_id)
    if draft is None or draft.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if not draft.body:
        raise HTTPException(status_code=409, detail="Draft has no body")

    if body.auto_approve or draft.status == "approved":
        draft.status = "approved"

    target = body.target_url.strip()
    if not target:
        # try to pull URL from brief
        for line in (draft.brief or "").splitlines():
            if line.lower().startswith("url:") or line.lower().startswith("target url:"):
                target = line.split(":", 1)[-1].strip()
                break

    title = ""
    for line in draft.body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line and len(line) < 90:
            title = line[:70]
            break

    change = SiteChange(
        customer_id=user.id,
        cms_connection_id=body.cms_connection_id,
        draft_id=draft.id,
        source="on_page" if draft.kind == "on-page-seo" else "ai",
        opportunity=f"Apply AI draft ({draft.kind}) to site",
        target_url=target,
        change_type=body.change_type,
        proposed={
            "title": title,
            "content": draft.body,
            "draftBody": draft.body,
            "metaDescription": "",
            "jev": draft.score,
            "changeType": body.change_type,
            "targetUrl": target,
        },
        status="approved" if body.auto_approve or draft.status == "approved" else "awaiting_approval",
    )
    db.add(change)
    db.commit()
    db.refresh(change)

    result = {"ok": True, "change": _change_row(change), "executed": None}
    if body.execute and change.status == "approved":
        # reuse execute logic
        exec_body = ExecuteBody(cms_connection_id=body.cms_connection_id, force_dry_run=body.force_dry_run)
        executed = execute_change(change.id, exec_body, db, user)
        result["executed"] = executed
        return result

    _sync_operator_features(db, user)
    return result


@router.post("/changes/{change_id}/approve")
def approve_change(change_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")
    row.status = "approved"
    row.updated_at = datetime.utcnow()
    row.approved_by = user.id
    row.approved_by_name = user.username or user.email or "Approver"
    row.approved_at = datetime.utcnow()
    if row.draft_id:
        draft = db.get(Draft, row.draft_id)
        if draft and draft.customer_id == user.id:
            draft.status = "approved"
    db.commit()
    db.refresh(row)
    _sync_operator_features(db, user)
    return _change_row(row)


@router.post("/changes/{change_id}/execute")
def execute_change(change_id: int, body: ExecuteBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")
    if row.status not in {"approved", "failed"}:
        raise HTTPException(status_code=409, detail=f"Approve first (status={row.status})")

    conn_id = body.cms_connection_id or row.cms_connection_id
    conn = db.get(CmsConnection, conn_id) if conn_id else None
    if conn is None or conn.customer_id != user.id:
        # fall back to any connected CMS
        conn = (
            db.query(CmsConnection)
            .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
            .order_by(CmsConnection.id.desc())
            .first()
        )
    if conn is None:
        raise HTTPException(status_code=400, detail="Connect WordPress, Shopify, Webflow, or a custom webhook first")

    proposed = dict(row.proposed or {})
    change_payload = {
        "title": proposed.get("title"),
        # v1 metadata-only — do not push full HTML content
        "metaDescription": proposed.get("metaDescription"),
        "remoteId": proposed.get("remoteId") or (conn.credentials or {}).get("defaultPostId"),
        "resource": proposed.get("resource") or "page",
        "changeType": row.change_type or "meta",
        "targetUrl": row.target_url,
        "beforeTitle": proposed.get("beforeTitle"),
        "collectionId": (conn.credentials or {}).get("collectionId"),
    }

    row.status = "executing"
    row.cms_connection_id = conn.id
    db.commit()

    creds = dict(conn.credentials or {})
    if body.force_dry_run:
        # strip secrets so adapters dry-run
        creds = {k: "" for k in creds}

    result = cms_connectors.apply_change(conn.provider, creds, conn.site_url, change_payload)
    row.execution = result
    row.updated_at = datetime.utcnow()
    conn.last_used_at = datetime.utcnow()

    if result.get("ok"):
        # Persist before/after for undo history
        before = result.get("before") or {}
        if before.get("title") or before.get("metaDescription"):
            proposed["beforeTitle"] = before.get("title") or proposed.get("beforeTitle") or ""
            proposed["beforeDescription"] = before.get("metaDescription") or proposed.get("beforeDescription") or ""
            row.proposed = proposed
        if result.get("remoteId"):
            proposed["remoteId"] = result["remoteId"]
            proposed["resource"] = result.get("resource") or proposed.get("resource")
            row.proposed = proposed

        if not result.get("dryRun"):
            row.applied_at = datetime.utcnow()
            row.status = "monitoring"
        else:
            row.status = "applied"
            row.applied_at = datetime.utcnow()
        row.monitoring = {
            "startedAt": datetime.utcnow().isoformat(timespec="seconds"),
            "baselinePosition": None,
            "latestPosition": None,
            "note": "Dry-run recorded" if result.get("dryRun") else "Live apply recorded — monitoring GSC",
            "dryRun": bool(result.get("dryRun")),
            "verified": result.get("verified"),
        }
        conn.last_error = ""
    else:
        row.status = "failed"
        conn.last_error = str(result.get("detail") or "execute failed")[:500]

    db.add(
        Job(
            customer_id=user.id,
            kind="cms_execute",
            status="done" if result.get("ok") else "error",
            detail={"changeId": row.id, "provider": conn.provider, "result": result},
        )
    )
    db.commit()
    db.refresh(row)
    _sync_operator_features(db, user)
    return {"ok": bool(result.get("ok")), "change": _change_row(row), "execution": result}


@router.post("/changes/{change_id}/monitor")
def monitor_change(change_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Refresh outcome using latest GSC position / page metrics for the target URL."""
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")

    pages = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind.in_(["top-pages", "position-tracking", "organic-search"]))
        .order_by(FeatureRecord.id.desc())
        .all()
    )
    latest_pos = None
    note = "No matching GSC row yet — Sync Google after the change indexes."
    target = (row.target_url or "").rstrip("/")
    for feat in pages:
        for r in (feat.payload or {}).get("rows") or []:
            cell = str(r[0] if r else "")
            if target and target in cell:
                # position often col 4 for pages, col 2 for position-tracking
                if feat.kind == "position-tracking" and len(r) > 2:
                    latest_pos = r[2]
                elif len(r) > 4:
                    latest_pos = r[4]
                note = f"Matched {feat.kind}"
                break
        if latest_pos is not None:
            break

    mon = dict(row.monitoring or {})
    if mon.get("baselinePosition") is None and latest_pos is not None:
        mon["baselinePosition"] = latest_pos
    mon["latestPosition"] = latest_pos
    mon["checkedAt"] = datetime.utcnow().isoformat(timespec="seconds")
    mon["note"] = note
    row.monitoring = mon
    if row.status in {"applied", "monitoring"} and latest_pos is not None:
        row.status = "monitoring"
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    _sync_operator_features(db, user)
    return _change_row(row)


@router.patch("/changes/{change_id}")
def patch_change(change_id: int, body: PatchChangeBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")
    proposed = dict(row.proposed or {})
    if body.title is not None:
        proposed["title"] = body.title.strip()[:120]
    if body.metaDescription is not None:
        proposed["metaDescription"] = body.metaDescription.strip()[:320]
    if body.remote_id is not None:
        proposed["remoteId"] = body.remote_id.strip()
    if body.resource is not None:
        proposed["resource"] = body.resource.strip()
    row.proposed = proposed
    if body.opportunity is not None:
        row.opportunity = body.opportunity.strip()
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _change_row(row)


@router.post("/changes/{change_id}/dismiss")
def dismiss_change(change_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")
    row.status = "dismissed"
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    _sync_operator_features(db, user)
    return _change_row(row)


@router.post("/changes/{change_id}/generate-meta")
def generate_meta(change_id: int, body: GenerateMetaBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")
    if not openai_configured():
        raise HTTPException(status_code=400, detail="OpenAI is not configured")

    profile = _profile_for(db, user.id)
    proposed = dict(row.proposed or {})
    organic = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "organic-search")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    query_terms = []
    if organic and (organic.payload or {}).get("rows"):
        query_terms = [str(q[0]).strip() for q in (organic.payload or {}).get("rows") if isinstance(q, (list, tuple)) and q][:8]
    from app.site_scan import host_of

    scan = _latest_scan(db, user.id, host_of(row.target_url or ""))
    page, site_ctx, scan_rivals = _site_context(scan, row.target_url or "")
    competitors = scan_rivals or proposed.get("competitors") or []
    if not competitors:
        try:
            from app import dataforseo

            seed = query_terms[0] if query_terms else ""
            competitors = dataforseo.competitor_serp(seed)
        except Exception:  # noqa: BLE001
            competitors = []
    taken = [
        (c.proposed or {}).get("title") or ""
        for c in db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id, SiteChange.id != row.id, SiteChange.status.in_(["awaiting_approval", "approved", "applied", "monitoring"]))
        .all()
        if host_of(c.target_url or "") == host_of(row.target_url or "")
    ]
    try:
        from app.meta_writer import write_title_description

        page_analytics = _analytics_for_page(db, user.id, row.target_url or "")
        drafted = write_title_description(
            url=row.target_url,
            profile={**_brief_profile(profile, (scan or {}).get("brief")), "voice": body.tone or profile.get("voice") or ""},
            gsc=(proposed.get("evidence") or {}),
            queries=query_terms,
            competitors=competitors,
            analytics=page_analytics,
            specificity=body.breadth or "balanced",
            page=page,
            site=site_ctx,
            taken=[t for t in taken if t][:20],
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from exc

    options = [
        {"title": drafted.get("title") or "", "description": drafted.get("metaDescription") or "", "reason": drafted.get("reason") or ""},
    ]
    proposed["title"] = drafted.get("title") or proposed.get("title") or ""
    proposed["metaDescription"] = drafted.get("metaDescription") or proposed.get("metaDescription") or ""
    proposed["reason"] = drafted.get("reason") or proposed.get("reason") or ""
    proposed["aiAlternatives"] = options
    proposed["model"] = drafted.get("model")
    proposed["role"] = drafted.get("role")
    proposed["draftBody"] = drafted.get("draftBody")
    proposed["competitors"] = [
        {k: r.get(k) for k in ("title", "url", "description", "source")} for r in (drafted.get("competitors") or competitors)[:6]
    ]
    proposed["target"] = drafted.get("target") or proposed.get("target") or ""
    proposed["analytics"] = bool((drafted.get("analytics") or {}).get("connected"))
    proposed["specificity"] = drafted.get("specificity") or body.breadth or "balanced"
    scraped = drafted.get("scraped") or {}
    if scraped.get("title"):
        proposed["beforeTitle"] = proposed.get("beforeTitle") or scraped.get("title")
    if scraped.get("description"):
        proposed["beforeDescription"] = proposed.get("beforeDescription") or scraped.get("description")
    row.proposed = proposed
    row.updated_at = datetime.utcnow()
    try:
        from app.routers.product import bump_usage

        bump_usage(db, user.id, openai=1)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return {"ok": True, "options": options, "model": drafted.get("model"), "change": _change_row(row)}


@router.post("/changes/{change_id}/undo")
def undo_change(change_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(SiteChange, change_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Change not found")
    if row.status not in {"applied", "monitoring", "closed"}:
        raise HTTPException(status_code=409, detail=f"Nothing to undo (status={row.status})")

    conn = db.get(CmsConnection, row.cms_connection_id) if row.cms_connection_id else None
    if conn is None or conn.customer_id != user.id:
        conn = (
            db.query(CmsConnection)
            .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
            .order_by(CmsConnection.id.desc())
            .first()
        )
    if conn is None:
        raise HTTPException(status_code=400, detail="Connect WordPress to undo live changes")

    proposed = dict(row.proposed or {})
    previous = {
        "title": proposed.get("beforeTitle") or "",
        "metaDescription": proposed.get("beforeDescription") or "",
        "beforeTitle": proposed.get("title") or "",
        "beforeDescription": proposed.get("metaDescription") or "",
    }
    if not previous["title"] and not previous["metaDescription"]:
        raise HTTPException(status_code=409, detail="No saved previous values for this change")

    change_payload = {
        "title": previous["title"],
        "metaDescription": previous["metaDescription"],
        "remoteId": proposed.get("remoteId") or (conn.credentials or {}).get("defaultPostId"),
        "resource": proposed.get("resource") or "page",
        "targetUrl": row.target_url,
        "changeType": "meta",
        "undo": True,
    }
    result = cms_connectors.undo_change(conn.provider, dict(conn.credentials or {}), conn.site_url, change_payload, previous)
    row.execution = {
        **(row.execution or {}),
        "undo": result,
        "undoneBy": user.username or user.email,
        "undoneAt": datetime.utcnow().isoformat(),
    }
    if result.get("ok"):
        row.status = "undone"
        row.monitoring = {**(row.monitoring or {}), "note": "Previous values restored", "undoneAt": datetime.utcnow().isoformat(timespec="seconds")}
        # swap proposed values so history shows restored state
        proposed["title"] = previous["title"]
        proposed["metaDescription"] = previous["metaDescription"]
        proposed["beforeTitle"] = previous["beforeTitle"]
        proposed["beforeDescription"] = previous["beforeDescription"]
        row.proposed = proposed
        conn.last_error = ""
    else:
        conn.last_error = str(result.get("detail") or "undo failed")[:500]
        raise HTTPException(status_code=502, detail=result.get("detail") or "Undo failed")

    row.updated_at = datetime.utcnow()
    conn.last_used_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    _sync_operator_features(db, user)
    return {"ok": True, "change": _change_row(row), "execution": result}


@router.get("/ai-visibility")
def get_ai_visibility(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    payload = ai_visibility.latest_report(db, user.id)
    return {"ok": True, "report": payload or None, "engines": ai_visibility.ENGINES}


@router.post("/ai-visibility")
def run_ai_visibility(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    profile = _profile_for(db, user.id)
    if not (profile.get("business") or "").strip() or not (profile.get("services") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Add your business name and services first (Profile), then run the AI visibility report.",
        )
    report = ai_visibility.run_visibility_report(db, user, profile)
    return {"ok": True, "report": report, "engines": ai_visibility.ENGINES}


@router.post("/ai-visibility/queue")
def queue_ai_visibility(body: VisibilityQueueBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = ai_visibility.latest_report(db, user.id) or {}
    check = next((c for c in (report.get("checks") or []) if str(c.get("id")) == body.checkId), None) if body.checkId else None
    solution = (check or {}).get("solution") or {}
    target = (body.targetUrl or solution.get("targetUrl") or "").strip()
    if not target:
        cms = (
            db.query(CmsConnection)
            .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
            .order_by(CmsConnection.id.desc())
            .first()
        )
        target = (cms.site_url if cms else "") or ""
    if not target:
        raise HTTPException(status_code=400, detail="Connect WordPress or pick a page URL before sending this fix to the work queue.")
    title = (body.title or solution.get("titleDraft") or "").strip()[:70]
    desc = (body.description or solution.get("descriptionDraft") or "").strip()[:170]
    opportunity = (
        body.opportunity
        or (check.get("error") if check else "")
        or solution.get("why")
        or "Improve AI answer visibility"
    )
    cms = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .order_by(CmsConnection.id.desc())
        .first()
    )
    change = SiteChange(
        customer_id=user.id,
        cms_connection_id=cms.id if cms else None,
        source="ai",
        opportunity=str(opportunity)[:500],
        target_url=target,
        change_type="meta",
        proposed={
            "title": title,
            "metaDescription": desc,
            "beforeTitle": "",
            "beforeDescription": "",
            "evidence": {"engine": (check or {}).get("engine"), "prompt": (check or {}).get("prompt")},
            "changeType": "meta",
            "targetUrl": target,
            "solutionSteps": (solution.get("steps") if isinstance(solution.get("steps"), list) else []),
        },
        status="awaiting_approval",
    )
    db.add(change)
    db.commit()
    db.refresh(change)
    _sync_operator_features(db, user)
    return {"ok": True, "change": _change_row(change)}


@router.post("/sync-features")
def sync_features(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _sync_operator_features(db, user)
    return {"ok": True}
