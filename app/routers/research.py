from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import research
from app.database import get_db
from app.models import User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class KeywordsBody(BaseModel):
    site: str = ""
    terms: list[str] = Field(default_factory=list)
    country: str = ""
    force: bool = False


class BacklinksBody(BaseModel):
    site: str = ""
    force: bool = False


class PromptItem(BaseModel):
    id: str | int | None = None
    text: str = ""
    engine: str = "ChatGPT"


class VisibilityBody(BaseModel):
    site: str = ""
    brand: str = ""
    country: str = ""
    prompts: list[PromptItem] = Field(default_factory=list)
    force: bool = False


def _run(fn, *args, **kwargs):
    try:
        return {"ok": True, **fn(*args, **kwargs)}
    except research.ResearchError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.get("/status")
def research_status(user: User = Depends(get_current_user)):
    _ = user
    return research.status()


@router.post("/keywords")
def research_keywords(body: KeywordsBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(research.keywords, db, user, site=body.site, terms=body.terms, country=body.country, force=body.force)


@router.post("/backlinks")
def research_backlinks(body: BacklinksBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(research.backlinks, db, user, site=body.site, force=body.force)


@router.post("/audit")
def research_audit(body: BacklinksBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(research.audit, db, user, site=body.site, force=body.force)


@router.post("/visibility")
def research_visibility(body: VisibilityBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(
        research.visibility,
        db,
        user,
        site=body.site,
        brand=body.brand,
        prompts=[p.model_dump() for p in body.prompts],
        country=body.country,
        force=body.force,
    )
