from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import quotas, research
from app.locations import LocationError
from app.database import get_db
from app.models import User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class KeywordsBody(BaseModel):
    site: str = ""
    terms: list[str] = Field(default_factory=list)
    country: str = ""
    reach: str = ""
    force: bool = False


class BacklinksBody(BaseModel):
    site: str = ""
    force: bool = False
    stored_only: bool = False


class PromptItem(BaseModel):
    id: str | int | None = None
    text: str = ""
    engine: str = "ChatGPT"


class VisibilityBody(BaseModel):
    site: str = ""
    brand: str = ""
    country: str = ""
    reach: str = ""
    prompts: list[PromptItem] = Field(default_factory=list)
    force: bool = False


def _run(fn, *args, **kwargs):
    try:
        return {"ok": True, **fn(*args, **kwargs)}
    except LocationError as exc:
        raise HTTPException(status_code=422, detail={"code": "location_required", "message": str(exc)}) from exc
    except quotas.QuotaError as exc:
        raise HTTPException(status_code=429, detail={"code": "quota_exceeded", "feature": exc.feature, "message": str(exc)}) from exc
    except research.ResearchError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc


@router.get("/usage")
def research_usage(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return quotas.summary(db, user)


@router.get("/status")
def research_status(user: User = Depends(get_current_user)):
    _ = user
    return research.status()


@router.post("/keywords")
def research_keywords(body: KeywordsBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(research.keywords, db, user, site=body.site, terms=body.terms, country=body.country, reach=body.reach, force=body.force)


@router.post("/backlinks")
def research_backlinks(body: BacklinksBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(research.backlinks, db, user, site=body.site, force=body.force, stored_only=body.stored_only)


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
        reach=body.reach,
        force=body.force,
    )
