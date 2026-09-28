"""SEO Operator — opportunity → AI change → approve → CMS execute → monitor."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import cms_connectors
from app.database import get_db
from app.models import CmsConnection, Draft, FeatureRecord, GoogleConnection, Job, SiteChange, User
from app.routers.google_connect import _connection_payload
from app.openai_client import generate_draft_body, openai_configured
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


class BusinessProfileBody(BaseModel):
    business: str = ""
    services: str = ""
    areas: str = ""
    locations: str = ""
    claims: str = ""
    voice: str = ""
    restrictions: str = ""
    rules: str = ""


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
    """Work-queue items come from Search Console. If Google is gone, leftover reviews must go."""
    if _google_is_connected(db, user.id):
        return 0
    cleared = _clear_user_queue(db, user.id)
    if cleared:
        db.commit()
    return cleared


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


@router.post("/changes/from-gsc")
def propose_from_gsc(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Pull top GSC pages and open metadata SiteChange opportunities (v1: titles & descriptions)."""
    from app.models import GoogleConnection

    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if google is None or not (google.gsc_site_url or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Connect Google Search Console first (Settings → Connections), then Sync. WordPress alone does not fill the work queue.",
        )

    pages = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "top-pages")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    organic = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "organic-search")
        .order_by(FeatureRecord.id.desc())
        .first()
    )

    def _is_live(rec: FeatureRecord | None) -> bool:
        if rec is None:
            return False
        payload = rec.payload or {}
        return payload.get("seedVersion") == LIVE or str(payload.get("source") or "").startswith("google")

    cms_rows = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .all()
    )
    homepage = _homepage_url(
        google.gsc_site_url,
        (pages.payload or {}).get("siteUrl") if pages else "",
        *(c.site_url for c in cms_rows),
    )

    if not _is_live(pages) and not _is_live(organic):
        try:
            from app.routers.google_connect import _valid_access_token
            from app import google_oauth as goauth

            token = _valid_access_token(db, google)
            live_pages = goauth.fetch_gsc_top_pages(token, google.gsc_site_url, days=28, row_limit=25)
            live_queries = goauth.fetch_gsc_top_queries(token, google.gsc_site_url, days=28, row_limit=25)
            pages = FeatureRecord(
                customer_id=user.id,
                kind="top-pages",
                title="Top Pages",
                payload={"rows": live_pages, "source": "google_search_console", "seedVersion": LIVE},
                status="stored",
            )
            organic = FeatureRecord(
                customer_id=user.id,
                kind="organic-search",
                title="Organic Search",
                payload={"rows": live_queries, "source": "google_search_console", "seedVersion": LIVE},
                status="stored",
            )
            db.add(pages)
            db.add(organic)
            db.flush()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"No live Search Console data yet. Sync Google first. {str(exc)[:160]}",
            ) from exc

    page_rows = list((pages.payload or {}).get("rows") or []) if _is_live(pages) else []
    queries = list((organic.payload or {}).get("rows") or []) if _is_live(organic) else []
    normalized = []
    for row in page_rows:
        url = _page_url(_row_key(row), homepage)
        if not url:
            continue
        if isinstance(row, list):
            normalized.append([url, *row[1:]])
        elif isinstance(row, tuple):
            normalized.append([url, *list(row[1:])])
        else:
            normalized.append([url, "0", "0", "0%", "0"])
    page_rows = normalized
    if not page_rows and google.gsc_site_url:
        try:
            from app.routers.google_connect import _valid_access_token
            from app import google_oauth as goauth

            token = _valid_access_token(db, google)
            for row in goauth.fetch_gsc_top_pages(token, google.gsc_site_url, days=28, row_limit=25):
                url = _page_url(_row_key(row), homepage)
                if url:
                    page_rows.append([url, *(list(row[1:]) if isinstance(row, (list, tuple)) else [])])
        except Exception:  # noqa: BLE001
            pass
    if not page_rows and homepage:
        page_rows = [[homepage, "0", "0", "0%", "0"]]
    if not page_rows:
        raise HTTPException(
            status_code=400,
            detail="Sync Google Search Console first — no live pages or site URL found yet.",
        )

    profile = _profile_for(db, user.id)
    biz = profile.get("business") or "your business"
    areas = profile.get("locations") or profile.get("areas") or ""
    services = profile.get("services") or ""
    claims = profile.get("claims") or ""
    voice = profile.get("voice") or ""
    restrictions = profile.get("restrictions") or profile.get("rules") or "No invented claims."

    # Avoid duplicating open queue items for the same URL
    open_urls = {
        (c.target_url or "").rstrip("/")
        for c in db.query(SiteChange)
        .filter(
            SiteChange.customer_id == user.id,
            SiteChange.status.in_(["proposed", "awaiting_approval", "approved", "dismissed"]),
        )
        .all()
    }

    created = []
    sources = page_rows[:8]
    cms_for_site = None
    gsc_url = (google.gsc_site_url or "").strip()
    cms_rows = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .all()
    )
    for cand in cms_rows:
        meta = cand.meta or {}
        if gsc_url and meta.get("gscSiteUrl") == gsc_url:
            cms_for_site = cand
            break
    if cms_for_site is None and len(cms_rows) == 1:
        cms_for_site = cms_rows[0]

    for page in sources:
        url = str(page[0] if page else "")
        target = url if url.startswith("http") else ""
        if not target:
            continue
        if target.rstrip("/") in open_urls:
            continue
        clicks = page[1] if len(page) > 1 else "—"
        impr = page[2] if len(page) > 2 else "—"
        pos = page[4] if len(page) > 4 else "—"
        opp = f"Make the search listing for {target} more specific"

        change = SiteChange(
            customer_id=user.id,
            cms_connection_id=cms_for_site.id if cms_for_site else None,
            source="gsc",
            opportunity=opp,
            target_url=target,
            change_type="meta",
            proposed={
                "title": "",
                "metaDescription": "",
                "beforeTitle": "",
                "beforeDescription": "",
                "evidence": {"clicks": clicks, "impressions": impr, "position": pos},
                "changeType": "meta",
                "targetUrl": target,
                "profileHints": {
                    "business": biz,
                    "areas": areas,
                    "locations": areas,
                    "services": services,
                    "claims": claims,
                    "voice": voice,
                    "restrictions": restrictions,
                },
            },
            status="awaiting_approval",
        )
        # AI draft titles/descriptions when OpenAI is configured
        if openai_configured():
            brief = (
                f"Business: {biz}\nServices: {services}\nLocations: {areas}\n"
                f"Approved claims: {claims or 'none'}\nBrand voice: {voice or 'clear and practical'}\n"
                f"Restrictions: {restrictions}\n"
                f"Page URL: {target or '(unknown)'}\nOpportunity: {opp}\n"
                f"GSC: impressions={impr}, clicks={clicks}, position={pos}\n\n"
                "Return exactly two lines:\nTITLE: <≤60 chars>\nDESCRIPTION: <≤155 chars>\n"
                "Factual only. Name the service and location when known. Never invent claims."
            )
            try:
                text, model, role = generate_draft_body(
                    "content-optimizer", brief, writing_type="Meta title & description", tokens=400
                )
                title, desc = "", ""
                for line in text.splitlines():
                    low = line.strip()
                    if low.upper().startswith("TITLE:"):
                        title = low.split(":", 1)[-1].strip()[:70]
                    elif low.upper().startswith("DESCRIPTION:"):
                        desc = low.split(":", 1)[-1].strip()[:170]
                if not title:
                    for line in text.splitlines():
                        line = line.strip().lstrip("#").strip()
                        if line and len(line) < 90:
                            title = line[:70]
                            break
                change.proposed = {
                    **(change.proposed or {}),
                    "title": title,
                    "metaDescription": desc,
                    "draftBody": text,
                    "model": model,
                    "role": role,
                }
            except Exception as exc:  # noqa: BLE001
                change.proposed = {**(change.proposed or {}), "aiError": str(exc)[:200]}

        db.add(change)
        db.flush()
        created.append(_change_row(change))
        if target:
            open_urls.add(target.rstrip("/"))

    if not created:
        db.commit()
        return {
            "ok": True,
            "created": 0,
            "changes": [],
            "reason": "Queue already covers these Search Console pages. Review existing items or dismiss them to refresh.",
        }

    db.commit()
    _sync_operator_features(db, user)
    return {"ok": True, "created": len(created), "changes": created}


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
    brief = (
        f"Business: {profile.get('business') or 'business'}\n"
        f"Services: {profile.get('services') or ''}\n"
        f"Locations: {profile.get('locations') or profile.get('areas') or ''}\n"
        f"Approved claims: {profile.get('claims') or 'none'}\n"
        f"Brand voice: {profile.get('voice') or 'clear and practical'}\n"
        f"Restrictions: {profile.get('restrictions') or profile.get('rules') or 'No invented claims.'}\n"
        f"Tone: {body.tone}\n"
        f"Page URL: {row.target_url}\n"
        f"Opportunity: {row.opportunity}\n"
        f"Current draft title: {proposed.get('title') or ''}\n"
        f"Current draft description: {proposed.get('metaDescription') or ''}\n\n"
        "Produce TWO alternative metadata options.\n"
        "Format exactly:\n"
        "OPTION 1\nTITLE: ...\nDESCRIPTION: ...\n"
        "OPTION 2\nTITLE: ...\nDESCRIPTION: ...\n"
    )
    try:
        text, model, role = generate_draft_body(
            "content-optimizer", brief, writing_type="Meta alternatives", tokens=500
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from exc

    options: list[dict] = []
    current: dict = {}
    for line in text.splitlines():
        low = line.strip()
        up = low.upper()
        if up.startswith("OPTION"):
            if current.get("title") or current.get("description"):
                options.append(current)
            current = {}
        elif up.startswith("TITLE:"):
            current["title"] = low.split(":", 1)[-1].strip()[:70]
        elif up.startswith("DESCRIPTION:"):
            current["description"] = low.split(":", 1)[-1].strip()[:170]
    if current.get("title") or current.get("description"):
        options.append(current)
    if not options:
        options = [{"title": proposed.get("title") or "", "description": proposed.get("metaDescription") or ""}]

    proposed["aiAlternatives"] = options
    proposed["model"] = model
    proposed["role"] = role
    row.proposed = proposed
    row.updated_at = datetime.utcnow()
    try:
        from app.routers.product import bump_usage

        bump_usage(db, user.id, openai=1)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return {"ok": True, "options": options, "model": model, "change": _change_row(row)}


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


@router.post("/sync-features")
def sync_features(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _sync_operator_features(db, user)
    return {"ok": True}
