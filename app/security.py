import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import JWT_HOURS, JWT_SECRET
from app.database import get_db
from app.models import User

bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 120_000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def make_token(user: User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=JWT_HOURS)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def make_password_token(user: User, purpose: str, minutes: int) -> str:
    """One-time link token for setting or resetting a password.

    It carries a fingerprint of the current password hash, so it stops working as soon as the
    password changes (single use) and when it expires.
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    payload = {
        "sub": str(user.id),
        "pw": purpose,
        "fp": hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16],
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def read_password_token(token: str, db: Session) -> User | None:
    try:
        payload = jwt.decode(token or "", JWT_SECRET, algorithms=["HS256"])
        user = db.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    if user is None or payload.get("pw") not in {"setup", "reset"}:
        return None
    if payload.get("fp") != hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]:
        return None
    return user


def user_type(role: str) -> str:
    if role == "ROLE_ADMIN":
        return "admin"
    if role == "ROLE_PARTNER":
        return "partner"
    return "client"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Sign in required")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User | None:
    if credentials is None:
        return None
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    return db.get(User, user_id)
