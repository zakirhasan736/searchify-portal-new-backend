from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Domain, Suggestion, Tag, User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1", tags=["catalog"])


class DomainBody(BaseModel):
    domain: str | None = None


class TextBody(BaseModel):
    text: str = ""


def tag_row(tag: Tag) -> dict:
    return {"id": tag.id, "name": tag.name, "value": tag.name, "label": tag.name}


@router.get("/suggestions/domains")
def list_domains(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [{"id": item.id, "name": item.name} for item in db.query(Domain).order_by(Domain.name).all()]


@router.post("/suggestions/tags")
def tags_for_domain(body: DomainBody, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    domain = db.query(Domain).filter(Domain.name == (body.domain or "")).one_or_none()
    if domain is None:
        return []
    tags = db.query(Tag).filter(Tag.domain_id == domain.id).order_by(Tag.name).all()
    return [tag_row(tag) for tag in tags]


@router.post("/suggestions/domain")
def suggestions_for_domain(body: DomainBody, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    domain = db.query(Domain).filter(Domain.name == (body.domain or "")).one_or_none()
    if domain is None:
        return []
    rows = db.query(Suggestion).filter(Suggestion.domain_id == domain.id).all()
    return [
        {
            "id": row.id,
            "title": row.title,
            "description": row.description,
            "relationships": row.relationships or [],
        }
        for row in rows
    ]


@router.post("/suggestions/text")
def text_variations(body: TextBody, _: User = Depends(get_current_user)):
    text = " ".join((body.text or "").split())
    if not text:
        return []
    short = text[:140].rstrip()
    return [text, short if short != text else f"{text}."]


@router.post("/tags")
def search_tags(body: list[str] | dict, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    names = body if isinstance(body, list) else []
    tags = db.query(Tag).order_by(Tag.name).all()
    if names:
        wanted = {name.lower() for name in names}
        tags = [tag for tag in tags if tag.name.lower() in wanted]
    return [tag_row(tag) for tag in tags]
