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


class TrackBody(BaseModel):
    site: str = ""
    keyword: str = ""
    # Page that should rank for the keyword (from the suggestion). Empty = Searchify picks it.
    page: str = ""
    concept: str = ""
    source: str = "manual"
    # Also write a new title + meta description for that page, waiting for approval.
    apply: bool = True


class UntrackBody(BaseModel):
    site: str = ""
    keyword: str = ""


@router.get("/keywords/tracked")
def tracked_keywords(site: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app import keyword_tracking

    host = research.host_of(site)
    if not host:
        raise problems.ProblemError(422, problems.make("site_required", "keywords", message="Add the website address in setup first."), {"code": "site_required", "message": "Add the website address in setup first."})
    saved = research._stored_keywords(db, user.id, host) or {}
    rows = keyword_tracking.tracked_payload(db, user.id, host, [*(saved.get("tracked") or []), *(saved.get("ranked") or [])])
    return {"ok": True, "host": host, "tracked": rows, "limit": quotas.tracked_keyword_limit(db, user)}


@router.post("/keywords/track")
def track_keyword(body: TrackBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Add a keyword to tracking (database) and, when asked, start the page update that targets it."""
    from app import keyword_tracking
    from app.concept_reads import match_keyword, site_concept

    host = research.host_of(body.site)
    term = keyword_tracking.clean_keyword(body.keyword)
    if not host or not term:
        message = "Add the website address in setup first." if not host else "Enter a keyword to track."
        raise problems.ProblemError(422, problems.make("site_required" if not host else "research_error", "keywords", message=message), {"code": "invalid", "message": message})
    concept = site_concept(db, user.id, host)
    fit = match_keyword(term, concept)
    page = body.page.strip() or research.ranked_url_for(db, user.id, host, term) or fit["page"]
    try:
        row, created = keyword_tracking.track(
            db, user.id, host, term,
            source=body.source, concept=body.concept or fit["concept"], page_url=page,
            limit=quotas.tracked_keyword_limit(db, user),
        )
    except ValueError as exc:
        raise problems.ProblemError(429, problems.make("quota_exceeded", "keywords", message=str(exc)), {"code": "tracked_limit", "message": str(exc)}) from exc
    warnings = []
    try:
        research.track_term_metrics(db, user, site=body.site, term=term)
    except (research.ResearchError, LocationError) as exc:
        warnings.append(f"Tracking was saved. Keyword numbers will appear on the next refresh: {exc}")
    if created:
        # First live Google check right away (desktop + mobile), so the keyword never sits on "measuring".
        from app import rank_tracking

        try:
            done = rank_tracking.check_site(db, user, host, keyword=term)
            if done.get("failed") and not done.get("checked"):
                warnings.append("The first live Google check failed. It runs again with the daily check.")
        except (research.ResearchError, LocationError) as exc:
            warnings.append(f"The first live Google check did not run: {exc}")
    applied = {"change": None, "note": ""}
    if body.apply:
        from app.routers.operator import keyword_page_change

        applied = keyword_page_change(db, user, keyword=term, page_url=row.page_url, concept=row.concept)
        if applied.get("change") and applied["change"].get("id"):
            row.change_id = applied["change"]["id"]
            db.commit()
    saved = research._stored_keywords(db, user.id, host) or {}
    rows = keyword_tracking.tracked_payload(db, user.id, host, [*(saved.get("tracked") or []), *(saved.get("ranked") or [])])
    return {
        "ok": True,
        "created": created,
        "keyword": term,
        "tracked": rows,
        "change": applied.get("change"),
        "note": applied.get("note") or "",
        "warnings": warnings,
    }


@router.post("/keywords/untrack")
def untrack_keyword(body: UntrackBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app import keyword_tracking

    host = research.host_of(body.site)
    removed = keyword_tracking.untrack(db, user.id, host, body.keyword)
    return {"ok": True, "removed": removed, "tracked": keyword_tracking.tracked_payload(db, user.id, host)}


class CheckRanksBody(BaseModel):
    site: str = ""
    keyword: str = ""


@router.post("/keywords/check-ranks")
def check_ranks(body: CheckRanksBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Live Google check now (DataForSEO SERP, desktop + mobile) for every tracked keyword, or one."""
    from app import rank_tracking

    host = research.host_of(body.site)
    result = _run("keywords", rank_tracking.check_site, db, user, host, keyword=body.keyword)
    return result
