from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import problems, quotas, research
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
    stored_only: bool = False


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


def _run(area: str, fn, *args, **kwargs):
    """Run a research call. Errors carry a structured ``problem`` (title, steps, action) for the
    help panel; ``detail`` keeps its old {code, message} shape so existing screens still work."""
    try:
        result = fn(*args, **kwargs)
    except LocationError as exc:
        detail = {"code": "location_required", "message": str(exc)}
        raise problems.ProblemError(422, problems.make("location_required", area, message=str(exc)), detail) from exc
    except quotas.QuotaError as exc:
        detail = {"code": "quota_exceeded", "feature": exc.feature, "message": str(exc)}
        raise problems.ProblemError(429, problems.make("quota_exceeded", area, message=str(exc)), detail) from exc
    except research.ResearchError as exc:
        code = getattr(exc, "code", None) or "research_error"
        detail = {"code": code, "message": str(exc)}
        problem = problems.make(code, area, message=str(exc), technical=getattr(exc, "technical", "") or "")
        raise problems.ProblemError(424, problem, detail) from exc
    return {"ok": True, **result, "warnings": problems.research_warnings(area, result)}


@router.get("/usage")
def research_usage(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return quotas.summary(db, user)


@router.get("/status")
def research_status(user: User = Depends(get_current_user)):
    _ = user
    return research.status()


@router.post("/keywords")
def research_keywords(body: KeywordsBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(
        "keywords",
        research.keywords,
        db,
        user,
        site=body.site,
        terms=body.terms,
        country=body.country,
        reach=body.reach,
        force=body.force,
        stored_only=body.stored_only,
    )


@router.post("/backlinks")
def research_backlinks(body: BacklinksBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run("backlinks", research.backlinks, db, user, site=body.site, force=body.force, stored_only=body.stored_only)


@router.post("/audit")
def research_audit(body: BacklinksBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run("site_audit", research.audit, db, user, site=body.site, force=body.force)


@router.post("/visibility")
def research_visibility(body: VisibilityBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _run(
        "ai_visibility",
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
