"""Agency workspace, usage controls, ops reliability, and defined reports."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    AgencyClient,
    CmsConnection,
    FeatureRecord,
    GoogleConnection,
    OpsNotice,
    ReportDefinition,
    SiteChange,
    User,
    WorkspaceMember,
)
from app.routers.operator import PLAN_LIMITS, _change_row, workspace_plan
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/product", tags=["product"])

REPORT_CONTENTS = [
    "gsc",
    "ga4",
    "pagespeed",
    "places",
    "queue",
    "history",
    "ads",
]


class ClientBody(BaseModel):
    name: str
    contact_email: str = ""
    notes: str = ""
    site_ids: list[int] = []


class MemberBody(BaseModel):
    email: str
    display_name: str = ""
    role: str = "viewer"


class MemberPatch(BaseModel):
    role: str | None = None
    status: str | None = None


class ControlsBody(BaseModel):
    scanHours: int | None = None
    costCapUsd: float | None = None
    scansPaused: bool | None = None


class ReportBody(BaseModel):
    name: str = "Weekly SEO report"
    contents: list[str] = ["gsc", "ga4", "pagespeed", "queue"]
    recipients: list[str] = []
    schedule: str = "weekly"


class NoticePatch(BaseModel):
    status: str = "acked"


def _controls(db: Session, user_id: int) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "workspace-controls")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    base = {
        "scanHours": 6,
        "costCapUsd": 0,
        "scansPaused": False,
        "apiCalls": {"gsc": 0, "ga4": 0, "pagespeed": 0, "places": 0, "ads": 0, "openai": 0},
        "lastBackupAt": "",
    }
    if row and isinstance(row.payload, dict):
        data = {**base, **row.payload}
        data["apiCalls"] = {**base["apiCalls"], **(row.payload.get("apiCalls") or {})}
        return data
    return base


def _save_controls(db: Session, user_id: int, payload: dict) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == "workspace-controls")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if row is None:
        row = FeatureRecord(customer_id=user_id, kind="workspace-controls", title="Workspace controls", payload=payload)
        db.add(row)
    else:
        row.payload = payload
    db.commit()
    return payload


def bump_usage(db: Session, user_id: int, **counts: int) -> None:
    data = _controls(db, user_id)
    calls = dict(data.get("apiCalls") or {})
    for key, value in counts.items():
        calls[key] = int(calls.get(key) or 0) + int(value or 0)
    data["apiCalls"] = calls
    _save_controls(db, user_id, data)


def _client_row(row: AgencyClient) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "contactEmail": row.contact_email,
        "notes": row.notes,
        "siteIds": row.site_ids or [],
        "createdAt": row.created_at.isoformat(),
    }


def _member_row(row: WorkspaceMember) -> dict:
    return {
        "id": row.id,
        "email": row.email,
        "displayName": row.display_name,
        "role": row.role,
        "status": row.status,
        "createdAt": row.created_at.isoformat(),
    }


def _report_row(row: ReportDefinition) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "contents": row.contents or [],
        "recipients": row.recipients or [],
        "schedule": row.schedule,
        "lastRunAt": row.last_run_at.isoformat() if row.last_run_at else None,
        "hasExport": bool(row.last_export),
        "createdAt": row.created_at.isoformat(),
    }


def _notice_row(row: OpsNotice) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "detail": row.detail,
        "status": row.status,
        "createdAt": row.created_at.isoformat(),
    }


def _ensure_owner_seat(db: Session, user: User) -> None:
    existing = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.owner_id == user.id, WorkspaceMember.role == "owner")
        .first()
    )
    if existing:
        return
    db.add(
        WorkspaceMember(
            owner_id=user.id,
            email=user.email,
            display_name=user.username,
            role="owner",
            status="active",
        )
    )
    db.commit()


def _refresh_notices(db: Session, user: User) -> None:
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if google and (google.last_error or google.status == "error"):
        open_sync = (
            db.query(OpsNotice)
            .filter(OpsNotice.customer_id == user.id, OpsNotice.kind == "sync", OpsNotice.status == "open")
            .first()
        )
        if open_sync is None:
            db.add(
                OpsNotice(
                    customer_id=user.id,
                    kind="sync",
                    title="Google sync failed",
                    detail=(google.last_error or "Search Console / GA4 sync needs a retry.")[:500],
                )
            )
    failed = (
        db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id, SiteChange.status == "failed")
        .count()
    )
    if failed:
        open_pub = (
            db.query(OpsNotice)
            .filter(OpsNotice.customer_id == user.id, OpsNotice.kind == "publish", OpsNotice.status == "open")
            .first()
        )
        if open_pub is None:
            db.add(
                OpsNotice(
                    customer_id=user.id,
                    kind="publish",
                    title=f"{failed} publish job(s) failed",
                    detail="Open Change history and retry the failed update.",
                )
            )
    db.commit()


def _usage_payload(db: Session, user: User) -> dict:
    controls = _controls(db, user.id)
    sites = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).count()
    pending = (
        db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id, SiteChange.status.in_(["proposed", "awaiting_approval", "approved"]))
        .count()
    )
    published = db.query(SiteChange).filter(SiteChange.customer_id == user.id, SiteChange.status.in_(["applied", "monitoring", "closed", "undone"])).count()
    calls = controls.get("apiCalls") or {}
    rates = {"gsc": 0.02, "ga4": 0.02, "pagespeed": 0.04, "places": 0.03, "ads": 0.02, "openai": 0.08}
    spend = round(sum(int(calls.get(k) or 0) * rates[k] for k in rates), 2)
    cap = float(controls.get("costCapUsd") or 0)
    return {
        **workspace_plan(user, sites),
        "sites": sites,
        "pendingApprovals": pending,
        "published": published,
        "scanHours": int(controls.get("scanHours") or 6),
        "scansPaused": bool(controls.get("scansPaused")),
        "costCapUsd": cap,
        "estimatedSpendUsd": spend,
        "overCap": cap > 0 and spend >= cap,
        "apiCalls": calls,
        "lastBackupAt": controls.get("lastBackupAt") or "",
        "plans": PLAN_LIMITS,
    }


@router.get("/workspace")
def product_workspace(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _ensure_owner_seat(db, user)
    _refresh_notices(db, user)
    clients = db.query(AgencyClient).filter(AgencyClient.owner_id == user.id).order_by(AgencyClient.id.desc()).all()
    members = db.query(WorkspaceMember).filter(WorkspaceMember.owner_id == user.id).order_by(WorkspaceMember.id).all()
    reports = db.query(ReportDefinition).filter(ReportDefinition.customer_id == user.id).order_by(ReportDefinition.id.desc()).all()
    notices = (
        db.query(OpsNotice)
        .filter(OpsNotice.customer_id == user.id)
        .order_by(OpsNotice.id.desc())
        .limit(20)
        .all()
    )
    sites = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).order_by(CmsConnection.id.desc()).all()
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    return {
        "clients": [_client_row(r) for r in clients],
        "members": [_member_row(r) for r in members],
        "reports": [_report_row(r) for r in reports],
        "notices": [_notice_row(r) for r in notices],
        "usage": _usage_payload(db, user),
        "sites": [{"id": s.id, "label": s.label, "siteUrl": s.site_url, "status": s.status} for s in sites],
        "google": {
            "connected": bool(google and (google.access_token or google.refresh_token)),
            "lastError": (google.last_error if google else "") or "",
            "lastSyncAt": google.last_sync_at.isoformat() if google and google.last_sync_at else None,
            "status": google.status if google else "disconnected",
        },
        "contentOptions": REPORT_CONTENTS,
        "roles": ["owner", "approver", "editor", "viewer"],
    }


@router.post("/clients")
def create_client(body: ClientBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Client name is required")
    row = AgencyClient(
        owner_id=user.id,
        name=name,
        contact_email=body.contact_email.strip(),
        notes=body.notes.strip(),
        site_ids=[int(i) for i in body.site_ids if i],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "client": _client_row(row)}


@router.patch("/clients/{client_id}")
def update_client(client_id: int, body: ClientBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(AgencyClient, client_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "Client not found")
    row.name = body.name.strip() or row.name
    row.contact_email = body.contact_email.strip()
    row.notes = body.notes.strip()
    row.site_ids = [int(i) for i in body.site_ids if i]
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "client": _client_row(row)}


@router.delete("/clients/{client_id}")
def delete_client(client_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(AgencyClient, client_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "Client not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/members")
def invite_member(body: MemberBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a team email")
    role = body.role if body.role in {"approver", "editor", "viewer"} else "viewer"
    row = WorkspaceMember(
        owner_id=user.id,
        email=email,
        display_name=body.display_name.strip() or email.split("@")[0],
        role=role,
        status="invited",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "member": _member_row(row)}


@router.patch("/members/{member_id}")
def patch_member(member_id: int, body: MemberPatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(WorkspaceMember, member_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "Member not found")
    if row.role == "owner":
        raise HTTPException(400, "The owner seat cannot change")
    if body.role in {"approver", "editor", "viewer"}:
        row.role = body.role
    if body.status in {"invited", "active"}:
        row.status = body.status
    db.commit()
    return {"ok": True, "member": _member_row(row)}


@router.delete("/members/{member_id}")
def remove_member(member_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(WorkspaceMember, member_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "Member not found")
    if row.role == "owner":
        raise HTTPException(400, "The owner seat cannot be removed")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/usage")
def get_usage(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _usage_payload(db, user)


@router.put("/usage/controls")
def put_controls(body: ControlsBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = _controls(db, user.id)
    if body.scanHours is not None:
        data["scanHours"] = max(1, min(int(body.scanHours), 168))
    if body.costCapUsd is not None:
        data["costCapUsd"] = max(0, float(body.costCapUsd))
    if body.scansPaused is not None:
        data["scansPaused"] = bool(body.scansPaused)
    _save_controls(db, user.id, data)
    return {"ok": True, "usage": _usage_payload(db, user)}


@router.get("/ops")
def get_ops(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _refresh_notices(db, user)
    notices = db.query(OpsNotice).filter(OpsNotice.customer_id == user.id).order_by(OpsNotice.id.desc()).limit(30).all()
    failed = (
        db.query(SiteChange)
        .filter(SiteChange.customer_id == user.id, SiteChange.status == "failed")
        .order_by(SiteChange.id.desc())
        .limit(10)
        .all()
    )
    controls = _controls(db, user.id)
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    return {
        "notices": [_notice_row(n) for n in notices],
        "failedPublishes": [_change_row(c) for c in failed],
        "scanHours": controls.get("scanHours") or 6,
        "scansPaused": bool(controls.get("scansPaused")),
        "lastBackupAt": controls.get("lastBackupAt") or "",
        "googleError": (google.last_error if google else "") or "",
        "googleStatus": google.status if google else "disconnected",
    }


@router.post("/ops/notices/{notice_id}")
def patch_notice(notice_id: int, body: NoticePatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(OpsNotice, notice_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(404, "Notice not found")
    if body.status not in {"open", "acked", "resolved"}:
        raise HTTPException(400, "Invalid status")
    row.status = body.status
    db.commit()
    return {"ok": True, "notice": _notice_row(row)}


@router.post("/ops/retry")
def retry_ops(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.routers.google_connect import sync_user_google

    retried = 0
    detail = ""
    google = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if google and (google.access_token or google.refresh_token):
        try:
            sync_user_google(db, user.id)
            retried += 1
            detail = "Google sync retried."
            for note in db.query(OpsNotice).filter(OpsNotice.customer_id == user.id, OpsNotice.kind == "sync", OpsNotice.status == "open"):
                note.status = "resolved"
            db.commit()
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)[:240]
            db.add(OpsNotice(customer_id=user.id, kind="retry", title="Retry failed", detail=detail))
            db.commit()
            raise HTTPException(502, detail) from exc
    return {"ok": True, "retried": retried, "detail": detail}


@router.post("/ops/backup")
def create_backup(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sites = db.query(CmsConnection).filter(CmsConnection.customer_id == user.id).all()
    clients = db.query(AgencyClient).filter(AgencyClient.owner_id == user.id).all()
    profile = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "business-profile")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    snapshot = {
        "at": datetime.utcnow().isoformat(),
        "sites": [{"id": s.id, "label": s.label, "siteUrl": s.site_url, "meta": s.meta or {}} for s in sites],
        "clients": [_client_row(c) for c in clients],
        "profile": (profile.payload if profile else {}) or {},
        "changeCount": db.query(SiteChange).filter(SiteChange.customer_id == user.id).count(),
    }
    existing = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "workspace-backup")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if existing is None:
        db.add(FeatureRecord(customer_id=user.id, kind="workspace-backup", title="Workspace backup", payload=snapshot))
    else:
        existing.payload = snapshot
    controls = _controls(db, user.id)
    controls["lastBackupAt"] = snapshot["at"]
    _save_controls(db, user.id, controls)
    db.add(OpsNotice(customer_id=user.id, kind="backup", title="Backup saved", detail=f"{len(sites)} sites · {snapshot['changeCount']} changes", status="resolved"))
    db.commit()
    return {"ok": True, "backup": {"at": snapshot["at"], "sites": len(sites), "changes": snapshot["changeCount"]}}


@router.post("/ops/restore")
def restore_backup(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "workspace-backup")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if row is None:
        raise HTTPException(404, "No backup yet. Create one first.")
    payload = row.payload or {}
    profile = payload.get("profile") or {}
    if profile:
        existing = (
            db.query(FeatureRecord)
            .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "business-profile")
            .order_by(FeatureRecord.id.desc())
            .first()
        )
        if existing:
            existing.payload = profile
        else:
            db.add(FeatureRecord(customer_id=user.id, kind="business-profile", title="Business Profile", payload=profile))
    db.add(
        OpsNotice(
            customer_id=user.id,
            kind="backup",
            title="Backup restored",
            detail=f"Restored business profile and site map from {payload.get('at') or 'the last snapshot'}. Live WordPress/Google tokens were not overwritten.",
            status="resolved",
        )
    )
    db.commit()
    return {"ok": True, "restoredAt": payload.get("at"), "sites": len(payload.get("sites") or [])}


@router.post("/reports")
def create_report(body: ReportBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    contents = [c for c in body.contents if c in REPORT_CONTENTS] or ["gsc"]
    schedule = body.schedule if body.schedule in {"manual", "weekly", "monthly"} else "manual"
    row = ReportDefinition(
        customer_id=user.id,
        name=body.name.strip() or "SEO report",
        contents=contents,
        recipients=[r.strip() for r in body.recipients if r.strip()],
        schedule=schedule,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "report": _report_row(row)}


@router.patch("/reports/{report_id}")
def update_report(report_id: int, body: ReportBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(ReportDefinition, report_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(404, "Report not found")
    row.name = body.name.strip() or row.name
    row.contents = [c for c in body.contents if c in REPORT_CONTENTS] or row.contents
    row.recipients = [r.strip() for r in body.recipients if r.strip()]
    if body.schedule in {"manual", "weekly", "monthly"}:
        row.schedule = body.schedule
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "report": _report_row(row)}


@router.delete("/reports/{report_id}")
def delete_report(report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(ReportDefinition, report_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(404, "Report not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/reports/{report_id}/run")
def run_report(report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(ReportDefinition, report_id)
    if row is None or row.customer_id != user.id:
        raise HTTPException(404, "Report not found")
    recs = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind.in_(["organic-search", "traffic-analytics", "site-audit", "google-ads", "local-competitors"]))
        .order_by(FeatureRecord.id.desc())
        .all()
    )
    latest: dict[str, dict] = {}
    for rec in recs:
        latest.setdefault(rec.kind, rec.payload or {})
    pending = db.query(SiteChange).filter(SiteChange.customer_id == user.id, SiteChange.status.in_(["proposed", "awaiting_approval", "approved"])).count()
    published = db.query(SiteChange).filter(SiteChange.customer_id == user.id, SiteChange.status.in_(["applied", "monitoring", "closed"])).count()
    lines = [f"Searchify report · {row.name}", f"Generated {datetime.utcnow().isoformat(timespec='seconds')}Z", f"Recipients: {', '.join(row.recipients) or 'workspace only'}", f"Schedule: {row.schedule}", ""]
    if "gsc" in row.contents:
        gsc = latest.get("organic-search") or {}
        lines += ["=== Search Console ===", gsc.get("summary") or "No GSC sync yet.", ""]
    if "ga4" in row.contents:
        ga = latest.get("traffic-analytics") or {}
        lines += ["=== GA4 ===", ga.get("summary") or "No GA4 sync yet.", ""]
    if "pagespeed" in row.contents:
        psi = latest.get("site-audit") or {}
        lines += ["=== PageSpeed ===", psi.get("summary") or "No PageSpeed run yet.", ""]
    if "places" in row.contents:
        places = latest.get("local-competitors") or {}
        lines += ["=== Places ===", places.get("summary") or "No Places sync yet.", ""]
    if "ads" in row.contents:
        ads = latest.get("google-ads") or {}
        lines += ["=== Google Ads ===", ads.get("summary") or "No Ads account linked.", ""]
    if "queue" in row.contents:
        lines += ["=== Work queue ===", f"Pending review: {pending}", ""]
    if "history" in row.contents:
        lines += ["=== Published ===", f"Applied updates: {published}", ""]
    text = "\n".join(lines)
    row.last_run_at = datetime.utcnow()
    row.last_export = text
    db.commit()
    return {"ok": True, "report": _report_row(row), "text": text}
