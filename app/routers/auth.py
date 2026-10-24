from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import hash_password, make_token, user_type, verify_password
from app.seed import ensure_project

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class SignInBody(BaseModel):
    username: str
    password: str


class SignUpBody(BaseModel):
    username: str
    password: str
    email: str
    roles: list[str] = ["ROLE_CLIENT"]


@router.post("/signin")
def sign_in(body: SignInBody, db: Session = Depends(get_db)):
    login = body.username.strip().lower()
    user = (
        db.query(User)
        .filter((func.lower(User.username) == login) | (func.lower(User.email) == login))
        .one_or_none()
    )
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Username or password is wrong")
    ensure_project(db, user)
    return {"token": make_token(user), "userType": user_type(user.role), "username": user.username}


def _session(db: Session, user: User) -> dict:
    ensure_project(db, user)
    return {
        "ok": True,
        "username": user.username,
        "userType": user_type(user.role),
        "token": make_token(user),
    }


@router.post("/signup")
def sign_up(body: SignUpBody, db: Session = Depends(get_db)):
    username = body.username.strip()
    email = body.email.strip().lower()
    if not username or "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a username and a valid email")
    by_name = db.query(User).filter(func.lower(User.username) == username.lower()).first()
    by_email = db.query(User).filter(func.lower(User.email) == email).first()
    if by_name and by_email and by_name.id != by_email.id:
        raise HTTPException(
            status_code=409,
            detail="That username and email belong to different accounts. Sign in with the email you already use.",
        )
    existing = by_email or by_name
    if existing:
        # Same person coming back through Create access: open the workspace when the password matches.
        if verify_password(body.password, existing.password_hash):
            return _session(db, existing)
        if by_email:
            raise HTTPException(
                status_code=409,
                detail="This email already has an account. Sign in, or use Forgot password to set a new one.",
            )
        raise HTTPException(status_code=409, detail="That username is taken. Choose another, or sign in.")
    role = "ROLE_ADMIN" if "ROLE_ADMIN" in body.roles else "ROLE_CLIENT"
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(body.password),
        role=role,
        plan="scale" if role == "ROLE_ADMIN" else "starter",
        site_limit=15 if role == "ROLE_ADMIN" else 5,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _session(db, user)
