from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.catalog_data import DRAFT_SEEDS, EXTRA, PANELS, SCREENS, SEED_VERSION, expand_sites, seed_rows
from app.database import get_db
from app.models import Domain, FeatureRecord, Tag, User
from app.security import get_current_user, hash_password

router = APIRouter(prefix="/api/v1", tags=["features"])

KINDS = {item[0] for item in list(SCREENS) + list(EXTRA)} | {
    "keyword-list",
    "keyword-share",
    "keyword-group",
    "website-keyword-list",
    "competitor-list",
    "profile",
    "google-services",
    "gsc-countries",
    "gsc-devices",
    "ga4-landing-pages",
    "ai-visibility",
    "local-listings",
    "local-seo",
    "google-ads",
    "paid-search",
    "business-profile",
    "workspace-controls",
    "workspace-backup",
} | set(DRAFT_SEEDS.keys())

KIND_ALIASES = {
    "ai-visibility": "ai-visibility-report",
    "local-seo": "local-listings",
}


class FeatureBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    payload: dict = {}
    status: str = "stored"


class ForgotBody(BaseModel):
    username: str
    password: str = Field(min_length=8)


class ProfileBody(BaseModel):
    displayName: str = ""
    photo: str = ""


class AdminTagBody(BaseModel):
    domain: str
    name: str


def row(record: FeatureRecord) -> dict:
    return {
        "id": record.id,
        "kind": record.kind,
        "title": record.title,
        "payload": record.payload or {},
        "status": record.status,
        "createdAt": record.created_at.isoformat(),
    }


def ensure_screen_rows(db: Session, user: User) -> None:
    records = db.query(FeatureRecord).filter(FeatureRecord.customer_id == user.id).all()
    by_title = {(item.kind, item.title): item for item in records}
    for kind, title, summary, columns, rows in list(SCREENS) + list(EXTRA):
        site_cols, site_rows = expand_sites(columns, rows)
        payload = {
            "summary": summary,
            "columns": site_cols,
            "rows": site_rows,
            "panels": PANELS.get(kind),
            "seedVersion": SEED_VERSION,
        }
        record = by_title.get((kind, title))
        if record is None:
            db.add(
                FeatureRecord(
                    customer_id=user.id,
                    kind=kind,
                    title=title,
                    payload=payload,
                    status="stored",
                )
            )
            continue
        current = record.payload or {}
        if current.get("seedVersion") == "google-live-v1" or str(current.get("source") or "").startswith("google"):
            continue
        current_rows = current.get("rows") or []
        current_cols = current.get("columns") or []
        missing_panels = bool(PANELS.get(kind)) and not current.get("panels")
        stale_version = current.get("seedVersion") != SEED_VERSION
        stale_dashboard = kind == "seo-dashboard" and not current.get("panels", {}).get("aiKpis")
        stale_shape = current_cols != list(site_cols) or (current_rows and site_rows and len(current_rows[0]) != len(site_rows[0]))
        if (
            stale_version
            or len(current_rows) < len(site_rows)
            or len(current_cols) < len(site_cols)
            or missing_panels
            or stale_dashboard
            or stale_shape
        ):
            record.payload = payload
            flag_modified(record, "payload")

    for kind, drafts in DRAFT_SEEDS.items():
        for draft in drafts:
            key = (kind, draft["title"])
            if key in by_title:
                continue
            payload = dict(draft["payload"])
            payload["seedVersion"] = SEED_VERSION
            db.add(
                FeatureRecord(
                    customer_id=user.id,
                    kind=kind,
                    title=draft["title"],
                    payload=payload,
                    status=draft.get("status") or "waiting_for_writer",
                )
            )

    # Prototype competitor + keyword lists for popups
    demo_lists = [
        (
            "competitor-list",
            "Core rivals",
            {"name": "Core rivals", "location": "Worldwide", "domains": ["astra-rank.example", "rivalmetrics.example", "growthcheck.example"], "seedVersion": SEED_VERSION},
        ),
        (
            "keyword-list",
            "Brand terms",
            {"list": "Brand terms", "limit": "12/100", "updated": "Prototype", "seedVersion": SEED_VERSION},
        ),
        (
            "keyword-group",
            "Content gaps group",
            {"name": "Content gaps group", "keyword": ["seo audit tool", "serp tracker", "content optimizer", "local seo software"], "seedVersion": SEED_VERSION},
        ),
    ]
    for kind, title, payload in demo_lists:
        if (kind, title) not in by_title:
            db.add(
                FeatureRecord(
                    customer_id=user.id,
                    kind=kind,
                    title=title,
                    payload=payload,
                    status="stored",
                )
            )

    db.commit()


def _is_live_payload(payload: dict | None) -> bool:
    p = payload or {}
    seed = str(p.get("seedVersion") or "")
    source = str(p.get("source") or "")
    return seed == "google-live-v1" or source.startswith("google") or source == "searchify_operator"


@router.get("/features/{kind}")
def list_features(kind: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    kind = KIND_ALIASES.get(kind, kind)
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="Unknown feature")
    records = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .limit(24)
        .all()
    )
    live = [record for record in records if _is_live_payload(record.payload)]
    live.sort(key=lambda record: -record.id)
    return [row(record) for record in live[:8]]


@router.post("/features/{kind}")
def create_feature(kind: str, body: FeatureBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="Unknown feature")
    payload = dict(body.payload)
    if kind == "keyword-group":
        keywords = payload.get("keyword") or payload.get("keywords") or []
        if len(keywords) > 200:
            raise HTTPException(status_code=400, detail="A keyword group holds up to 200 keywords")
    if kind == "competitor-list":
        domains = payload.get("domains") or []
        flat = domains[0] if domains and isinstance(domains[0], list) else domains
        if isinstance(flat, list) and len(flat) > 20:
            raise HTTPException(status_code=400, detail="A competitor list holds up to 20 domains")
    record = FeatureRecord(
        customer_id=user.id,
        kind=kind,
        title=body.title.strip(),
        payload=payload,
        status=body.status,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return row(record)


@router.delete("/features/{kind}/{record_id}")
def delete_feature(kind: str, record_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = db.get(FeatureRecord, record_id)
    if record is None or record.customer_id != user.id or record.kind != kind:
        raise HTTPException(status_code=404, detail="Record not found")
    db.delete(record)
    db.commit()
    return {"ok": True}


@router.post("/auth/forgot-password")
def forgot_password(body: ForgotBody, db: Session = Depends(get_db)):
    login = body.username.strip()
    user = db.query(User).filter((User.username == login) | (User.email == login)).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="No account uses that username")
    user.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@router.get("/profile")
def get_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "profile")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    payload = record.payload if record else {}
    return {
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "displayName": payload.get("displayName") or user.username,
        "photo": payload.get("photo") or "",
    }


@router.put("/profile")
def save_profile(body: ProfileBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = FeatureRecord(
        customer_id=user.id,
        kind="profile",
        title="Profile",
        payload={"displayName": body.displayName or user.username, "photo": body.photo},
        status="stored",
    )
    db.add(record)
    db.commit()
    return {"ok": True, "displayName": record.payload["displayName"]}


@router.get("/admin/tags")
def admin_tags(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "ROLE_ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    domains = db.query(Domain).order_by(Domain.name).all()
    result = []
    for domain in domains:
        tags = db.query(Tag).filter(Tag.domain_id == domain.id).order_by(Tag.name).all()
        result.append({"id": domain.id, "name": domain.name, "tags": [{"id": tag.id, "name": tag.name} for tag in tags]})
    return result


@router.post("/admin/tags")
def add_admin_tag(body: AdminTagBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "ROLE_ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    domain = db.query(Domain).filter(Domain.name == body.domain).one_or_none()
    if domain is None:
        domain = Domain(name=body.domain.strip())
        db.add(domain)
        db.flush()
    tag = Tag(domain_id=domain.id, name=body.name.strip())
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return {"id": tag.id, "name": tag.name, "domain": domain.name}
