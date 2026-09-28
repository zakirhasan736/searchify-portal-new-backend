"""Social login: Google + GitHub → Searchify JWT."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.models import User
from app.security import hash_password, make_token, user_type
from app.seed import ensure_project

router = APIRouter(prefix="/api/v1/auth/oauth", tags=["auth-oauth"])


def _login_state(provider: str) -> str:
    payload = {
        "purpose": "login",
        "provider": provider,
        "nonce": secrets.token_urlsafe(8),
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def _parse_login_state(state: str) -> dict:
    data = jwt.decode(state, config.JWT_SECRET, algorithms=["HS256"])
    if data.get("purpose") != "login":
        raise ValueError("bad purpose")
    return data


def _username_from_email(email: str, provider: str) -> str:
    base = (email.split("@")[0] or provider).lower()
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._-")[:40] or provider
    return base


def _upsert_oauth_user(db: Session, *, email: str, name: str, provider: str) -> User:
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Social login did not return an email")
    user = db.query(User).filter(User.email == email).one_or_none()
    if user:
        ensure_project(db, user)
        return user
    username = _username_from_email(email, provider)
    if db.query(User).filter(User.username == username).first():
        username = f"{username}_{secrets.token_hex(2)}"
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(24)),
        role="ROLE_CLIENT",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_project(db, user)
    return user


def _portal_token_redirect(user: User) -> RedirectResponse:
    token = make_token(user)
    q = urlencode(
        {
            "social": "1",
            "token": token,
            "userType": user_type(user.role),
            "username": user.username,
        }
    )
    return RedirectResponse(f"{config.PUBLIC_SITE_URL}/signin?{q}")


@router.get("/providers")
def oauth_providers():
    return {
        "google": bool(config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET),
        "github": bool(config.GITHUB_CLIENT_ID and config.GITHUB_CLIENT_SECRET),
    }


@router.get("/google/start")
def google_login_start():
    if not (config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET):
        raise HTTPException(status_code=503, detail="Google login is not configured")
    redirect = config.GOOGLE_LOGIN_REDIRECT_URI or config.GOOGLE_OAUTH_REDIRECT_URI
    params = {
        "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": _login_state("google"),
    }
    return {"authUrl": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"}


@router.get("/google/callback")
def google_login_callback(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    fail = f"{config.PUBLIC_SITE_URL}/signin?social=error"
    if error or not code or not state:
        return RedirectResponse(f"{fail}&detail={error or 'missing_code'}")
    try:
        _parse_login_state(state)
    except Exception:  # noqa: BLE001
        return RedirectResponse(f"{fail}&detail=bad_state")
    redirect = config.GOOGLE_LOGIN_REDIRECT_URI or config.GOOGLE_OAUTH_REDIRECT_URI
    try:
        with httpx.Client(timeout=30.0) as client:
            token_res = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
                    "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
                    "redirect_uri": redirect,
                    "grant_type": "authorization_code",
                },
            )
            if token_res.status_code >= 400:
                return RedirectResponse(f"{fail}&detail=token")
            access = token_res.json().get("access_token") or ""
            info = client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access}"},
            ).json()
        user = _upsert_oauth_user(
            db,
            email=info.get("email") or "",
            name=info.get("name") or "",
            provider="google",
        )
        return _portal_token_redirect(user)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"{fail}&detail={str(exc)[:80]}")


@router.get("/github/start")
def github_login_start():
    if not (config.GITHUB_CLIENT_ID and config.GITHUB_CLIENT_SECRET):
        raise HTTPException(status_code=503, detail="GitHub login is not configured")
    params = {
        "client_id": config.GITHUB_CLIENT_ID,
        "redirect_uri": config.GITHUB_OAUTH_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": _login_state("github"),
    }
    return {"authUrl": f"https://github.com/login/oauth/authorize?{urlencode(params)}"}


@router.get("/github/callback")
def github_login_callback(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    fail = f"{config.PUBLIC_SITE_URL}/signin?social=error"
    if error or not code or not state:
        return RedirectResponse(f"{fail}&detail={error or 'missing_code'}")
    try:
        _parse_login_state(state)
    except Exception:  # noqa: BLE001
        return RedirectResponse(f"{fail}&detail=bad_state")
    try:
        with httpx.Client(timeout=30.0, headers={"Accept": "application/json"}) as client:
            token_res = client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": config.GITHUB_CLIENT_ID,
                    "client_secret": config.GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": config.GITHUB_OAUTH_REDIRECT_URI,
                },
            )
            access = token_res.json().get("access_token") or ""
            if not access:
                return RedirectResponse(f"{fail}&detail=token")
            headers = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
            profile = client.get("https://api.github.com/user", headers=headers).json()
            emails = client.get("https://api.github.com/user/emails", headers=headers).json()
        email = ""
        if isinstance(emails, list):
            primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
            email = (primary or emails[0]).get("email") if emails else ""
        if not email:
            email = profile.get("email") or ""
        if not email and profile.get("login"):
            email = f"{profile['login']}@users.noreply.github.com"
        user = _upsert_oauth_user(
            db,
            email=email,
            name=profile.get("name") or profile.get("login") or "",
            provider="github",
        )
        return _portal_token_redirect(user)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"{fail}&detail={str(exc)[:80]}")
