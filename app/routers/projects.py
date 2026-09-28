from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, User
from app.security import get_current_user
from app.seed import project_payload

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _owned(db: Session, user: User, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/customer/{customer_id}")
def project_for_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.id != customer_id and user.role != "ROLE_ADMIN":
        raise HTTPException(status_code=403, detail="Not allowed")
    project = db.query(Project).filter(Project.customer_id == customer_id).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_payload(project)


@router.get("/{project_id}")
def project_by_id(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return project_payload(_owned(db, user, project_id))


@router.put("/{project_id}")
def update_project(
    project_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned(db, user, project_id)
    websites = body.get("websites") if isinstance(body, dict) else None
    payload = dict(project.payload or {})
    if isinstance(websites, list):
        payload["websites"] = websites
    if isinstance(body.get("name"), str):
        payload["name"] = body["name"]
    project.payload = payload
    db.commit()
    db.refresh(project)
    return project_payload(project)
