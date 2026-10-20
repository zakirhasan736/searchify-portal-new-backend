"""Subscribe a phone and send the three alerts: waiting fix, failed publish, weekly report."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PushSubscription, User
from app.push_alerts import notify_weekly, vapid_keys
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/push", tags=["push"])


class SubscribeBody(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2000)
    keys: dict = Field(default_factory=dict)


@router.get("/public-key")
def public_key(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        public, _private = vapid_keys(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Phone alerts are not available until the push library is installed.") from exc
    return {"publicKey": public}


@router.post("/subscribe")
def subscribe(body: SubscribeBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p256dh = str(body.keys.get("p256dh") or "").strip()
    auth = str(body.keys.get("auth") or "").strip()
    if not p256dh or not auth:
        raise HTTPException(status_code=400, detail="The browser subscription is missing its keys.")
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == body.endpoint).first()
    if row is None:
        row = PushSubscription(customer_id=user.id, endpoint=body.endpoint, p256dh=p256dh, auth=auth)
        db.add(row)
    else:
        row.customer_id = user.id
        row.p256dh = p256dh
        row.auth = auth
    db.commit()
    return {"ok": True}


@router.delete("/subscribe")
def unsubscribe(body: SubscribeBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = (
        db.query(PushSubscription)
        .filter(PushSubscription.customer_id == user.id, PushSubscription.endpoint == body.endpoint)
        .first()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.post("/weekly")
def weekly(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return notify_weekly(db, user.id)
