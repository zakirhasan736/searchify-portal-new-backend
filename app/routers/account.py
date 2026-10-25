"""Password reset and change for every account type: customers, partners and HQ admins.

- POST /api/v1/auth/forgot           email a single-use reset link (never says whether the account exists)
- POST /api/v1/auth/reset            set a new password from that link and sign in
- POST /api/v1/auth/change-password  signed-in: current + new password; signs out other sessions
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import config, mailer
from app.database import get_db
from app.models import User
from app.security import (
    get_current_user,
    hash_password,
    make_password_token,
    make_token,
    read_password_token,
    user_type,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["account"])
log = logging.getLogger("searchify.account")

RESET_MINUTES = 30
MIN_LENGTH = 8
AREA_ROLE = {"customer": "ROLE_CLIENT", "partner": "ROLE_PARTNER", "admin": "ROLE_ADMIN"}
AREA_LINK = {
    "customer": "/reset-password?token={token}",
    "partner": "/partners/login?reset={token}",
    "admin": "/admin/login?reset={token}",
}
AREA_NAME = {"customer": "Searchify", "partner": "Searchify partner", "admin": "Searchify HQ admin"}
SAME_ANSWER = "If that email has an account here, a reset link is on its way. It works for 30 minutes."


class ForgotBody(BaseModel):
    email: str
    area: str = "customer"  # customer | partner | admin


class ResetBody(BaseModel):
    token: str
    password: str


class ChangeBody(BaseModel):
    current: str
    password: str


def password_problem(password: str) -> str:
    if len(password or "") < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if (password or "").strip() != password:
        return "The password can't start or end with a space."
    return ""


def _area_of(user: User) -> str:
    return {"ROLE_ADMIN": "admin", "ROLE_PARTNER": "partner"}.get(user.role, "customer")


@router.post("/forgot")
def forgot(body: ForgotBody, db: Session = Depends(get_db)):
    area = body.area if body.area in AREA_ROLE else "customer"
    login = (body.email or "").strip().lower()
    user = (
        db.query(User)
        .filter((func.lower(User.email) == login) | (func.lower(User.username) == login))
        .one_or_none()
    )
    # Only the sign-in page for that account type sends its link (an admin reset never goes to /login).
    if user is not None and user.role == AREA_ROLE[area] and user.email:
        token = make_password_token(user, "reset", RESET_MINUTES)
        link = config.PUBLIC_SITE_URL + AREA_LINK[area].format(token=token)
        sent = mailer.send(
            user.email,
            f"Reset your {AREA_NAME[area]} password",
            f"Someone asked to reset the password for your {AREA_NAME[area]} account ({user.email}).\n\n"
            f"Choose a new password here. The link works once, for {RESET_MINUTES} minutes:\n{link}\n\n"
            "If you did not ask for this, ignore this email. Your password stays the same.",
        )
        if not sent:
            log.warning("password reset requested for %s (%s) but email is not configured", user.email, area)
    return {"ok": True, "detail": SAME_ANSWER, "emailConfigured": mailer.configured()}


@router.post("/reset")
def reset(body: ResetBody, db: Session = Depends(get_db)):
    user = read_password_token(body.token, db)
    if user is None:
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    user.password_hash = hash_password(body.password)
    db.add(user)
    db.commit()
    return {
        "ok": True,
        "token": make_token(user),
        "userType": user_type(user.role),
        "username": user.username,
        "area": _area_of(user),
    }


@router.post("/change-password")
def change_password(body: ChangeBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not verify_password(body.current or "", user.password_hash):
        raise HTTPException(400, "Your current password is not right.")
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    if verify_password(body.password, user.password_hash):
        raise HTTPException(400, "Choose a password you are not already using.")
    user.password_hash = hash_password(body.password)
    db.add(user)
    db.commit()
    # Every older session for this account stops working; this device gets a fresh one.
    return {"ok": True, "token": make_token(user), "userType": user_type(user.role), "username": user.username}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "email": user.email, "userType": user_type(user.role)}
