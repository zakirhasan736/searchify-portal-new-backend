"""Temporary users against the dev Postgres. Every row they create is deleted afterwards."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import text

from app.database import SessionLocal
from app.models import User

TABLES = ("site_changes", "jobs", "feature_records", "cms_connections", "google_connections", "drafts")


def make_user(prefix: str = "pytest") -> SimpleNamespace:
    tag = uuid.uuid4().hex[:10]
    with SessionLocal() as db:
        row = User(username=f"{prefix}_{tag}", email=f"{prefix}_{tag}@example.test", password_hash="x", role="ROLE_CLIENT", plan="starter")
        db.add(row)
        db.commit()
        return SimpleNamespace(id=row.id, username=row.username, email=row.email, role=row.role, plan=row.plan, site_limit=5)


def drop_user(user_id: int) -> None:
    with SessionLocal() as db:
        for table in TABLES:
            try:
                db.execute(text(f"DELETE FROM {table} WHERE customer_id = :u"), {"u": user_id})
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        db.commit()
