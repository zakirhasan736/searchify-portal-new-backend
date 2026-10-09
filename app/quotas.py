"""Monthly per-plan limits on paid research calls. Only real provider calls count; cached answers are free."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import FeatureRecord, User

FEATURES = {
    "keywords": "Keyword research refreshes",
    "backlinks": "Backlink refreshes",
    "visibility": "AI visibility checks",
    "competitors": "Competitor lookups",
    "audits": "Site audits",
}

PLANS = {
    "starter": {
        "label": "Starter",
        "trackedKeywords": 10,
        "keywordIdeas": 10,
        "monthly": {"keywords": 10, "backlinks": 5, "visibility": 20, "competitors": 10, "audits": 2},
    },
    "growth": {
        "label": "Growth",
        "trackedKeywords": 50,
        "keywordIdeas": 25,
        "monthly": {"keywords": 40, "backlinks": 20, "visibility": 100, "competitors": 40, "audits": 8},
    },
    "agency": {
        "label": "Agency",
        "trackedKeywords": 200,
        "keywordIdeas": 50,
        "monthly": {"keywords": 150, "backlinks": 60, "visibility": 400, "competitors": 150, "audits": 30},
    },
}
SERVER_PLAN = {"starter": "starter", "growth": "growth", "agency": "agency", "scale": "agency", "custom": "agency"}
KIND = "dfs-quota"
LOCK_BASE = 7_400_000


class QuotaError(Exception):
    def __init__(self, feature: str, limit: int, plan: str):
        self.feature, self.limit, self.plan = feature, limit, plan
        super().__init__(
            f"Your {PLANS[plan]['label']} plan includes {limit} {FEATURES[feature].lower()} a month, and they are used. "
            f"Saved results stay available. The allowance resets on {next_reset().date().isoformat()}."
        )


def period(now: datetime | None = None) -> str:
    return (now or datetime.utcnow()).strftime("%Y-%m")


def next_reset(now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)


def plan_for(db: Session, user: User) -> str | None:
    """None means no limit (admins). The plan chosen in setup wins, then the account plan."""
    if getattr(user, "role", "") == "ROLE_ADMIN":
        return None
    from app import site_profiles

    chosen = ((site_profiles.get_journey(db, user.id) or {}).get("state") or {}).get("planId")
    if chosen in PLANS:
        return chosen
    return SERVER_PLAN.get((getattr(user, "plan", "") or "starter").lower(), "starter")


def _row(db: Session, user_id: int, when: str) -> FeatureRecord | None:
    return (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == KIND, FeatureRecord.title == f"Quota {when}")
        .order_by(FeatureRecord.id.desc())
        .first()
    )


def used(db: Session, user_id: int) -> dict:
    row = _row(db, user_id, period())
    counts = dict((row.payload if row else {}) or {})
    return {feature: int(counts.get(feature) or 0) for feature in FEATURES}


def left(db: Session, user: User, feature: str) -> int | None:
    plan = plan_for(db, user)
    if plan is None:
        return None
    return max(0, PLANS[plan]["monthly"][feature] - used(db, user.id)[feature])


def check(db: Session, user: User, feature: str, calls: int = 1) -> None:
    plan = plan_for(db, user)
    if plan is None:
        return
    limit = PLANS[plan]["monthly"][feature]
    if used(db, user.id)[feature] + calls > limit:
        raise QuotaError(feature, limit, plan)


def consume(db: Session, user: User, feature: str, calls: int = 1) -> None:
    if calls <= 0:
        return
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_BASE + int(user.id)})
    when = period()
    row = _row(db, user.id, when)
    counts = dict((row.payload if row else {}) or {})
    counts[feature] = int(counts.get(feature) or 0) + calls
    if row is None:
        db.add(FeatureRecord(customer_id=user.id, kind=KIND, title=f"Quota {when}", payload=counts, status="stored"))
    else:
        row.payload = counts
    db.commit()


def tracked_keyword_limit(db: Session, user: User) -> int | None:
    plan = plan_for(db, user)
    return None if plan is None else PLANS[plan]["trackedKeywords"]


def keyword_idea_limit(db: Session, user: User) -> int:
    plan = plan_for(db, user)
    return 50 if plan is None else PLANS[plan]["keywordIdeas"]


def summary(db: Session, user: User) -> dict:
    plan = plan_for(db, user)
    counts = used(db, user.id)
    limits = PLANS[plan]["monthly"] if plan else {}
    return {
        "plan": plan or "unlimited",
        "planLabel": PLANS[plan]["label"] if plan else "Admin (no limit)",
        "period": period(),
        "resetsOn": next_reset().date().isoformat(),
        "trackedKeywords": PLANS[plan]["trackedKeywords"] if plan else None,
        "keywordIdeas": PLANS[plan]["keywordIdeas"] if plan else None,
        "features": {
            feature: {
                "label": label,
                "used": counts[feature],
                "limit": limits.get(feature),
                "left": None if not plan else max(0, limits[feature] - counts[feature]),
            }
            for feature, label in FEATURES.items()
        },
        "plans": PLANS,
    }
