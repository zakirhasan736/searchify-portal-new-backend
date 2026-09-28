from sqlalchemy.orm import Session

from app.models import Domain, Project, Suggestion, Tag, User
from app.security import hash_password


CATALOG = {
    "Retail": ["Store", "Product", "Checkout"],
    "Education": ["Course", "Campus", "Enrollment"],
    "Publisher": ["Article", "Author", "Newsletter"],
    "Automotive": ["Dealer", "Model", "Service"],
    "IT software": ["Product", "Docs", "Pricing"],
    "Healthcare": ["Clinic", "Patient", "Appointment"],
    "Import": ["Supplier", "Catalog", "Shipping"],
}


def project_payload(project: Project) -> dict:
    payload = dict(project.payload or {})
    payload["projectId"] = project.id
    payload["customerId"] = str(project.customer_id)
    payload.setdefault("websites", [])
    return payload


def ensure_project(db: Session, user: User) -> Project:
    project = db.query(Project).filter(Project.customer_id == user.id).one_or_none()
    if project:
        return project
    project = Project(
        customer_id=user.id,
        payload={"name": "My works", "websites": []},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def seed(db: Session) -> None:
    if db.query(User).count() == 0:
        db.add_all(
            [
                User(
                    username="client",
                    email="client@searchify.local",
                    password_hash=hash_password("Client#1234"),
                    role="ROLE_CLIENT",
                    plan="starter",
                    site_limit=5,
                ),
                User(
                    username="admin",
                    email="admin@searchify.local",
                    password_hash=hash_password("Admin#1234"),
                    role="ROLE_ADMIN",
                    plan="scale",
                    site_limit=15,
                ),
            ]
        )
        db.commit()
        for user in db.query(User).all():
            ensure_project(db, user)
    for user in db.query(User).all():
        if user.role == "ROLE_ADMIN" and (user.plan or "starter") == "starter" and int(user.site_limit or 5) == 5:
            user.plan = "scale"
            user.site_limit = 15
            db.add(user)
    db.commit()

    if db.query(Domain).count() > 0:
        return

    for domain_name, tag_names in CATALOG.items():
        domain = Domain(name=domain_name)
        db.add(domain)
        db.flush()
        tags = []
        for tag_name in tag_names:
            tag = Tag(domain_id=domain.id, name=tag_name)
            db.add(tag)
            tags.append(tag)
        db.flush()
        for tag in tags:
            db.add(
                Suggestion(
                    domain_id=domain.id,
                    title=f"{tag.name} page title",
                    description=f"A {domain_name.lower()} page focused on {tag.name.lower()}.",
                    relationships=[{"label": tag.name}],
                )
            )
    db.commit()
