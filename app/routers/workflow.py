from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CompetitorList, Draft, Job, KeywordList, User
from app.openai_client import generate_draft_body, openai_configured
from app.security import get_current_user

router = APIRouter(prefix="/api/v1", tags=["workflow"])


class JobBody(BaseModel):
    kind: str
    detail: dict = {}


class DraftBody(BaseModel):
    kind: str
    brief: str = ""
    project_id: int | None = None
    writing_type: str = ""
    tokens: int | None = None
    force_astra: bool = False


class KeywordListBody(BaseModel):
    name: str = Field(min_length=4)
    keywords: list[str] = Field(default_factory=list, max_length=200)


class CompetitorListBody(BaseModel):
    name: str = Field(min_length=1)
    country: str = ""
    domains: list[str] = Field(default_factory=list, max_length=20)


def job_row(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "detail": job.detail,
        "createdAt": job.created_at.isoformat(),
    }


def draft_row(draft: Draft, *, model: str | None = None, role: str | None = None) -> dict:
    data = {
        "id": draft.id,
        "projectId": draft.project_id,
        "kind": draft.kind,
        "brief": draft.brief,
        "body": draft.body,
        "status": draft.status,
        "score": draft.score,
        "createdAt": draft.created_at.isoformat(),
    }
    if model:
        data["model"] = model
    if role:
        data["role"] = role
    return data


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Job).filter(Job.customer_id == user.id).order_by(Job.id.desc()).limit(50).all()
    return [job_row(row) for row in rows]


@router.post("/jobs")
def create_job(body: JobBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    allowed = {"sync", "crawl", "index", "report", "draft"}
    if body.kind not in allowed:
        raise HTTPException(status_code=400, detail="Unknown job kind")
    status = "queued"
    detail = dict(body.detail)
    if body.kind in {"report", "draft", "index", "sync"}:
        if body.kind == "draft" and openai_configured():
            status = "queued"
            detail["note"] = "OpenAI is configured. Use POST /drafts to generate text."
        else:
            status = "waiting_for_provider"
            detail["note"] = "Saved in PostgreSQL. The provider for this job is not connected yet."
    job = Job(customer_id=user.id, kind=body.kind, status=status, detail=detail)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job_row(job)


@router.get("/drafts")
def list_drafts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Draft).filter(Draft.customer_id == user.id).order_by(Draft.id.desc()).limit(50).all()
    return [draft_row(row) for row in rows]


@router.post("/drafts")
def create_draft(body: DraftBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.jev import score_draft

    body_text = ""
    status = "waiting_for_writer"
    job_status = "waiting_for_provider"
    job_detail: dict = {"draftKind": body.kind}
    model_used = ""
    role_used = ""
    jev: dict = {}

    if openai_configured():
        try:
            body_text, model_used, role_used = generate_draft_body(
                body.kind,
                body.brief,
                writing_type=body.writing_type or None,
                tokens=body.tokens,
                force_astra=body.force_astra,
            )
            jev = score_draft(body.kind, body.brief, body_text)
            if jev.get("band") == "auto":
                status = "approved"
                job_detail["note"] = "JEV auto-safe — draft approved for human publish review."
            elif jev.get("band") == "reject":
                status = "waiting_for_writer"
                job_detail["note"] = "JEV rejected score — rewrite before approve."
            else:
                status = "waiting_for_writer"
                job_detail["note"] = "JEV inbox — person must approve before publish."
            job_status = "done"
            job_detail.update(
                {
                    "model": model_used,
                    "role": role_used,
                    "provider": "openai",
                    "jev": jev,
                }
            )
        except Exception as exc:  # noqa: BLE001
            job_status = "error"
            job_detail.update(
                {
                    "note": "OpenAI call failed. Draft saved empty for retry.",
                    "error": str(exc)[:500],
                    "provider": "openai",
                }
            )
    else:
        job_detail["note"] = "OPENAI_API_KEY is not set. No draft text was generated."

    draft = Draft(
        customer_id=user.id,
        project_id=body.project_id,
        kind=body.kind,
        brief=body.brief,
        status=status,
        body=body_text,
        score=jev or None,
    )
    db.add(draft)
    db.add(
        Job(
            customer_id=user.id,
            kind="draft",
            status=job_status,
            detail=job_detail,
        )
    )
    db.commit()
    db.refresh(draft)
    data = draft_row(draft, model=model_used or None, role=role_used or None)
    data["jev"] = jev
    return data


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    draft = db.get(Draft, draft_id)
    if draft is None or draft.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if not draft.body:
        raise HTTPException(status_code=409, detail="Nothing to approve until a draft exists")
    draft.status = "approved"
    db.commit()
    return draft_row(draft)


@router.get("/keyword-lists")
def list_keywords(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(KeywordList).filter(KeywordList.customer_id == user.id).order_by(KeywordList.id.desc()).all()
    return [{"id": row.id, "name": row.name, "keywords": row.keywords} for row in rows]


@router.post("/keyword-lists")
def create_keyword_list(body: KeywordListBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = KeywordList(customer_id=user.id, name=body.name.strip(), keywords=body.keywords[:200])
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "keywords": row.keywords}


@router.get("/competitor-lists")
def list_competitors(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(CompetitorList).filter(CompetitorList.customer_id == user.id).order_by(CompetitorList.id.desc()).all()
    return [{"id": row.id, "name": row.name, "country": row.country, "domains": row.domains} for row in rows]


@router.post("/competitor-lists")
def create_competitor_list(body: CompetitorListBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = CompetitorList(
        customer_id=user.id,
        name=body.name.strip(),
        country=body.country,
        domains=body.domains[:20],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "country": row.country, "domains": row.domains}
