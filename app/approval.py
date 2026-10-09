"""Rules for approving and publishing a title/description change.

- Only the exact text a person approved can be published. Any edit after approval needs a new approval.
- Before publishing, the live page is read again. If its title or description changed since the
  reviewer saw it, the change goes back to review instead of overwriting someone else's edit.
- A change is claimed for publishing with one conditional UPDATE, so two clicks cannot publish twice.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import SiteChange

EDITABLE = {"proposed", "awaiting_approval", "approved", "failed", "needs_review"}
APPROVABLE = {"proposed", "awaiting_approval", "approved", "failed", "needs_review"}
PUBLISHABLE = ("approved", "failed")
DISMISSABLE = {"proposed", "awaiting_approval", "approved", "failed", "needs_review"}
PUBLISHED = {"executing", "applied", "monitoring", "closed", "published_unverified"}


class ApprovalError(Exception):
    def __init__(self, message: str, status: int = 409, code: str = "conflict"):
        super().__init__(message)
        self.status = status
        self.code = code


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def content_hash(title: str | None, description: str | None) -> str:
    return hashlib.sha256(f"{_norm(title)}\n{_norm(description)}".encode()).hexdigest()


def listing_changed(seen: dict, live: dict) -> list[str]:
    """Which of title/description differ between what the reviewer saw and the live page."""
    out = []
    for key in ("title", "description"):
        if _norm(seen.get(key)) != _norm(live.get(key)):
            out.append(key)
    return out


def stamp_approval(row: SiteChange, user) -> None:
    proposed = dict(row.proposed or {})
    title = (proposed.get("title") or "").strip()
    desc = (proposed.get("metaDescription") or "").strip()
    if row.status not in APPROVABLE:
        raise ApprovalError(f"This change is {row.status.replace('_', ' ')} and cannot be approved.")
    if (row.change_type or "meta") == "meta" and (not title or not desc):
        raise ApprovalError("Add a title and a description before approving.", status=422, code="incomplete")
    proposed["approvedHash"] = content_hash(title, desc)
    proposed["approvedTitle"] = title
    proposed["approvedDescription"] = desc
    proposed.pop("sourceChanged", None)
    row.proposed = proposed
    row.status = "approved"
    row.approved_by = user.id
    row.approved_by_name = user.username or user.email or "Approver"
    row.approved_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()


def apply_edit(row: SiteChange, *, title: str | None, description: str | None) -> bool:
    """Save an edit. Returns True if an approval was withdrawn because the text changed."""
    if row.status not in EDITABLE:
        raise ApprovalError(f"This change is {row.status.replace('_', ' ')} and can no longer be edited.")
    proposed = dict(row.proposed or {})
    if title is not None:
        proposed["title"] = re.sub(r"\s+", " ", title).strip()[:120]
    if description is not None:
        proposed["metaDescription"] = re.sub(r"\s+", " ", description).strip()[:320]
    withdrawn = False
    if proposed.get("approvedHash") and content_hash(proposed.get("title"), proposed.get("metaDescription")) != proposed["approvedHash"]:
        for key in ("approvedHash", "approvedTitle", "approvedDescription"):
            proposed.pop(key, None)
        if row.status == "approved" or row.status == "failed":
            row.status = "awaiting_approval"
            row.approved_at = None
        withdrawn = True
    row.proposed = proposed
    row.updated_at = datetime.utcnow()
    return withdrawn


def check_publishable(row: SiteChange) -> dict:
    """The approved title/description, or ApprovalError. Does not change the row."""
    if row.status in PUBLISHED:
        raise ApprovalError("This change was already published or is publishing now.", code="already_published")
    if row.status not in PUBLISHABLE:
        raise ApprovalError(f"Approve this change first (it is {row.status.replace('_', ' ')}).", code="not_approved")
    proposed = row.proposed or {}
    approved = proposed.get("approvedHash")
    if not approved:
        raise ApprovalError("This change has no recorded approval. Approve it again.", code="not_approved")
    if content_hash(proposed.get("title"), proposed.get("metaDescription")) != approved:
        raise ApprovalError("The text changed after approval. Review and approve it again.", code="edited_after_approval")
    return {"title": proposed.get("approvedTitle") or proposed.get("title"), "metaDescription": proposed.get("approvedDescription") or proposed.get("metaDescription")}


def send_back_for_review(row: SiteChange, live: dict, changed: list[str]) -> None:
    proposed = dict(row.proposed or {})
    proposed["sourceChanged"] = {
        "fields": changed,
        "seen": {"title": proposed.get("beforeTitle") or "", "description": proposed.get("beforeDescription") or ""},
        "live": {"title": live.get("title") or "", "description": live.get("description") or ""},
        "at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    proposed["beforeTitle"] = live.get("title") or ""
    proposed["beforeDescription"] = live.get("description") or ""
    for key in ("approvedHash", "approvedTitle", "approvedDescription"):
        proposed.pop(key, None)
    row.proposed = proposed
    row.status = "awaiting_approval"
    row.approved_at = None
    row.updated_at = datetime.utcnow()


def claim_for_publish(db: Session, row: SiteChange) -> bool:
    """Atomically move approved/failed -> executing. False if another request got there first."""
    result = db.execute(
        update(SiteChange)
        .where(SiteChange.id == row.id, SiteChange.customer_id == row.customer_id, SiteChange.status.in_(PUBLISHABLE))
        .values(status="executing", updated_at=datetime.utcnow())
    )
    db.commit()
    db.refresh(row)
    return bool(result.rowcount)
