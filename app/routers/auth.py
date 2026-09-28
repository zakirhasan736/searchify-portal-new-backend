from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
    login = body.username.strip()
    user = (
        db.query(User)
        .filter((User.username == login) | (User.email == login))
        .one_or_none()
    )
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Username or password is wrong")
    ensure_project(db, user)
    return {"token": make_token(user), "userType": user_type(user.role), "username": user.username}


@router.post("/signup")
def sign_up(body: SignUpBody, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Username is already registered")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="Email is already registered")
    role = "ROLE_ADMIN" if "ROLE_ADMIN" in body.roles else "ROLE_CLIENT"
    user = User(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        role=role,
        plan="scale" if role == "ROLE_ADMIN" else "starter",
        site_limit=15 if role == "ROLE_ADMIN" else 5,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_project(db, user)
    return {"ok": True, "username": user.username, "userType": user_type(user.role)}
