import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

import logging

from app import config, google_match
from app.database import get_db
from app import google_ads as gads
from app import google_oauth as goauth
from app.models import CmsConnection, FeatureRecord, GoogleConnection, Job, SiteChange, User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1", tags=["google"])
log = logging.getLogger("searchify.google")
REAUTH = "Google access was revoked or has expired. Reconnect Google to keep syncing."


class SelectBody(BaseModel):
    gsc_site_url: str | None = None
    ga4_property_id: str | None = None
    ga4_property_name: str | None = None
    cms_connection_id: int | None = None


class SyncBody(BaseModel):
    cms_connection_id: int | None = None


class GoogleAccountBody(BaseModel):
    email: str = ""


class AdsSelectBody(BaseModel):
    customer_id: str
    customer_name: str = ""
    cms_connection_id: int | None = None
    login_customer_id: str | None = None


class PageSpeedBody(BaseModel):
    url: str | None = None
    strategy: str = "mobile"


class PlacesBody(BaseModel):
    query: str | None = None
    max_results: int = 10


LIVE = "google-live-v1"


def _ensure_google_config():
    if not goauth.google_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured in .env")


def _friendly_google_error(exc: Exception) -> str:
    detail = str(exc)
    low = detail.lower()
    if "getaddrinfo" in low or "name or service not known" in low or "failed to resolve" in low:
        return "Could not reach Google APIs (DNS/network). Check internet, then Sync again."
    if "object is not bound" in low or "objectdeletederror" in low:
        return "Google was disconnected during sync. Connect Google again, then Sync."
    return detail[:500]


def _connection_payload(row: GoogleConnection | None) -> dict:
    if row is None:
        return {
            "connected": False,
            "status": "waiting_for_provider",
            "googleEmail": "",
            "gscSiteUrl": "",
            "ga4PropertyId": "",
            "ga4PropertyName": "",
            "adsConfigured": gads.ads_configured(),
            "adsScope": False,
            "adsCustomerId": "",
            "adsCustomerName": "",
            "scopes": [],
            "lastSyncAt": None,
            "lastError": "",
            "account": {"status": "disconnected", "email": ""},
            "gsc": {"status": "not_selected", "siteUrl": "", "checkedAt": None},
            "ga4": {"status": "not_selected", "propertyId": "", "propertyName": "", "checkedAt": None},
        }
    meta = row.meta or {}
    has_tokens = bool(row.access_token or row.refresh_token)
    validated = meta.get("validated") or {}
    account_status = "reauth_required" if row.status == "reauth_required" else ("connected" if has_tokens else "disconnected")

    def property_status(selected: str, key: str) -> str:
        if account_status != "connected":
            return account_status if selected else "not_selected"
        if not selected:
            return "not_selected"
        stamp = validated.get(key) or {}
        return "connected" if stamp.get("value") == selected else "unverified"

    return {
        "account": {"status": account_status, "email": row.google_email or ""},
        "gsc": {"status": property_status(row.gsc_site_url or "", "gsc"), "siteUrl": row.gsc_site_url or "",
                "checkedAt": (validated.get("gsc") or {}).get("at")},
        "ga4": {"status": property_status(row.ga4_property_id or "", "ga4"), "propertyId": row.ga4_property_id or "",
                "propertyName": row.ga4_property_name or "", "checkedAt": (validated.get("ga4") or {}).get("at")},
        "connected": has_tokens and account_status == "connected",
        "status": row.status,
        "googleEmail": row.google_email,
        "gscSiteUrl": row.gsc_site_url,
        "ga4PropertyId": row.ga4_property_id,
        "ga4PropertyName": row.ga4_property_name,
        "adsConfigured": gads.ads_configured(),
        "adsScope": goauth.ads_scope_granted(row.scopes),
        "adsCustomerId": meta.get("adsCustomerId") or "",
        "adsCustomerName": meta.get("adsCustomerName") or "",
        "scopes": row.scopes or [],
        "lastSyncAt": row.last_sync_at.isoformat() if row.last_sync_at else None,
        "lastError": row.last_error or "",
        "accounts": _public_accounts(row),
    }


def _public_accounts(row: GoogleConnection) -> list[dict]:
    saved = (row.meta or {}).get("accounts") or {}
    active = (row.google_email or "").lower()
    found = []
    seen = set()
    for key, item in saved.items():
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or key or "").strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        found.append({"email": email, "active": email.lower() == active and bool(row.access_token or row.refresh_token)})
    if row.google_email and row.google_email.lower() not in seen and (row.access_token or row.refresh_token):
        found.append({"email": row.google_email, "active": True})
    return found


def _remember_account(row: GoogleConnection) -> None:
    email = (row.google_email or "").strip()
    if not email or not (row.refresh_token or row.access_token):
        return
    meta = dict(row.meta or {})
    accounts = dict(meta.get("accounts") or {})
    accounts[email.lower()] = {
        "email": email,
        "accessToken": row.access_token or "",
        "refreshToken": row.refresh_token or "",
        "tokenExpiry": row.token_expiry.isoformat() if row.token_expiry else "",
        "scopes": list(row.scopes or []),
        "gscSiteUrl": row.gsc_site_url or "",
        "ga4PropertyId": row.ga4_property_id or "",
        "ga4PropertyName": row.ga4_property_name or "",
    }
    meta["accounts"] = accounts
    row.meta = meta
    flag_modified(row, "meta")


def _apply_saved_account(row: GoogleConnection, saved: dict) -> None:
    row.access_token = saved.get("accessToken") or ""
    row.refresh_token = saved.get("refreshToken") or ""
    raw_expiry = saved.get("tokenExpiry") or ""
    try:
        row.token_expiry = datetime.fromisoformat(raw_expiry) if raw_expiry else None
    except ValueError:
        row.token_expiry = None
    row.scopes = list(saved.get("scopes") or [])
    row.google_email = saved.get("email") or row.google_email
    row.gsc_site_url = saved.get("gscSiteUrl") or ""
    row.ga4_property_id = saved.get("ga4PropertyId") or ""
    row.ga4_property_name = saved.get("ga4PropertyName") or ""
    row.status = "connected"
    row.last_error = ""
    row.updated_at = datetime.utcnow()


def _valid_access_token(db: Session, row: GoogleConnection) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row.access_token and row.token_expiry and row.token_expiry > now + timedelta(minutes=2):
        return row.access_token
    if not row.refresh_token:
        _mark_reauth(db, row)
    try:
        data = goauth.refresh_access_token(row.refresh_token)
    except goauth.GoogleReauthRequired:
        _mark_reauth(db, row)
    row.access_token = data.get("access_token") or row.access_token
    expires_in = int(data.get("expires_in") or 3600)
    row.token_expiry = now + timedelta(seconds=expires_in)
    if data.get("refresh_token"):
        row.refresh_token = data["refresh_token"]
    row.updated_at = now
    db.commit()
    return row.access_token


def _mark_reauth(db: Session, row: GoogleConnection):
    row.status = "reauth_required"
    row.access_token = ""
    row.token_expiry = None
    row.last_error = REAUTH
    row.updated_at = datetime.utcnow()
    db.commit()
    log.warning("google_reauth_required", extra={"user_id": row.customer_id})
    raise HTTPException(status_code=401, detail={"code": "google_reauth_required", "message": REAUTH})


def _stamp_validated(row: GoogleConnection, key: str, value: str) -> None:
    meta = dict(row.meta or {})
    validated = dict(meta.get("validated") or {})
    if value:
        validated[key] = {"value": value, "at": datetime.utcnow().isoformat(timespec="seconds")}
    else:
        validated.pop(key, None)
    meta["validated"] = validated
    row.meta = meta
    flag_modified(row, "meta")


def _ga4_with_streams(token: str, properties: list[dict]) -> list[dict]:
    out = []
    for prop in properties[:25]:
        out.append({**prop, "streams": goauth.list_ga4_streams(token, prop["propertyId"])})
    return out + [{**p, "streams": []} for p in properties[25:]]


def _upsert_feature(db: Session, user_id: int, kind: str, title: str, payload: dict, status: str = "stored"):
    cms_id = db.info.get("cms_connection_id")
    if cms_id and isinstance(payload, dict) and "cmsConnectionId" not in payload:
        payload = {**payload, "cmsConnectionId": cms_id}
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind, FeatureRecord.title == title)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if row is None:
        row = FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload, status=status)
        db.add(row)
    else:
        row.payload = payload
        row.status = status
    db.commit()


@router.get("/oauth/google/status")
def google_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    return {
        "configured": goauth.google_configured(),
        "redirectUri": config.GOOGLE_OAUTH_REDIRECT_URI,
        "connection": _connection_payload(row),
    }


@router.get("/oauth/google/start")
def google_start(
    services: str = Query(default="gsc,ga4,ads"),
    add: str = Query(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _ensure_google_config()
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    already = bool(row and (row.refresh_token or row.access_token))
    adding = add == "1"
    if already and not adding and goauth.ads_scope_granted(row.scopes) and "ads" in (services or ""):
        return {
            "authUrl": "",
            "alreadyConnected": True,
            "adsScope": True,
            "services": services,
        }
    state = goauth.make_oauth_state(user.id, services=services)
    prompt = "select_account consent" if adding else ""
    return {
        "authUrl": goauth.auth_url(state, services=services, force_consent=not already or adding, prompt=prompt),
        "alreadyConnected": already,
        "adsScope": goauth.ads_scope_granted(row.scopes) if row else False,
        "services": services,
        "redirectUri": config.GOOGLE_OAUTH_REDIRECT_URI,
    }


@router.get("/oauth/google/callback")
def google_callback(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    portal = config.PUBLIC_SITE_URL.rstrip("/")
    fail = f"{portal}/app/connections?google=error"

    if error:
        return RedirectResponse(f"{fail}&detail={error}")
    if not code or not state:
        return RedirectResponse(f"{fail}&detail=missing_code")

    try:
        payload = goauth.parse_oauth_state(state)
        user_id = int(payload["uid"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return RedirectResponse(f"{fail}&detail=bad_state")

    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse(f"{fail}&detail=user")

    try:
        tokens = goauth.exchange_code(code)
        access = tokens.get("access_token") or ""
        refresh = tokens.get("refresh_token") or ""
        expires_in = int(tokens.get("expires_in") or 3600)
        info = goauth.fetch_userinfo(access) if access else {}
        now = datetime.utcnow()

        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
        if row is None:
            row = GoogleConnection(customer_id=user.id)
            db.add(row)

        new_email = (info.get("email") or "").strip()
        switching = bool(row.google_email and new_email and row.google_email.lower() != new_email.lower())
        saved_new = ((row.meta or {}).get("accounts") or {}).get(new_email.lower()) if switching else {}
        if switching:
            _remember_account(row)

        row.access_token = access
        if refresh:
            row.refresh_token = refresh
        row.token_expiry = now + timedelta(seconds=expires_in)
        new_scopes = [s for s in (tokens.get("scope") or "").split() if s]
        prev_scopes = [] if switching else [s for s in (row.scopes or []) if s]
        row.scopes = list(dict.fromkeys(prev_scopes + new_scopes))
        row.google_email = new_email or row.google_email or ""
        if switching:
            row.gsc_site_url = (saved_new or {}).get("gscSiteUrl") or ""
            row.ga4_property_id = (saved_new or {}).get("ga4PropertyId") or ""
            row.ga4_property_name = (saved_new or {}).get("ga4PropertyName") or ""
        row.status = "connected"
        row.last_error = ""
        row.updated_at = now
        _remember_account(row)
        db.commit()
        _auto_match_after_connect(db, row)

        db.add(
            Job(
                customer_id=user.id,
                kind="sync",
                status="done",
                detail={"provider": "google", "note": "OAuth connected", "email": row.google_email, "services": payload.get("services")},
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"{fail}&detail={str(exc)[:120]}")

    services = str(payload.get("services") or "")
    flag = "ads-connected" if "ads" in services else "connected"
    q = urlencode({"google": flag, "email": row.google_email or ""})
    dest = f"{portal}/app/connections/ads" if flag == "ads-connected" else f"{portal}/app/connections"
    return RedirectResponse(f"{dest}?{q}")


def _auto_match_after_connect(db: Session, row: GoogleConnection) -> None:
    """If the workspace has exactly one website and no property is chosen, pick the property only when the match is unambiguous."""
    if row.gsc_site_url and row.ga4_property_id:
        return
    try:
        from app import site_profiles

        journey = site_profiles.get_journey(db, row.customer_id) or {}
        sites = [s for s in (journey.get("state") or {}).get("sites") or [] if s.get("status") == "ready" and (s.get("answers") or {}).get("site")]
        if len(sites) != 1:
            return
        site = sites[0]["answers"]["site"]
        token = row.access_token
        if not row.gsc_site_url:
            found = google_match.match_gsc(site, goauth.list_gsc_sites(token))
            if found["status"] == "matched":
                row.gsc_site_url = found["selected"]
                _stamp_validated(row, "gsc", found["selected"])
        if not row.ga4_property_id:
            found = google_match.match_ga4(site, _ga4_with_streams(token, goauth.list_ga4_properties(token)))
            if found["status"] == "matched":
                row.ga4_property_id = found["selected"]
                row.ga4_property_name = found.get("selectedName") or ""
                _stamp_validated(row, "ga4", found["selected"])
        _remember_account(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("google_auto_match_failed", extra={"user_id": row.customer_id, "error": type(exc).__name__})


@router.get("/oauth/google/sites")
def google_sites(site: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Properties this Google account can read, plus the best match for `site` when one is clear."""
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row is None or not (row.access_token or row.refresh_token):
        raise HTTPException(status_code=404, detail="Connect Google first")
    token = _valid_access_token(db, row)
    try:
        gsc = goauth.list_gsc_sites(token)
        ga4 = goauth.list_ga4_properties(token)
    except Exception as exc:  # noqa: BLE001
        row.last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=_friendly_google_error(exc)[:300]) from exc
    out = {"gscSites": gsc, "ga4Properties": ga4, "account": row.google_email or ""}
    if site:
        ga4_full = _ga4_with_streams(token, ga4)
        out["ga4Properties"] = ga4_full
        out["match"] = {"gsc": google_match.match_gsc(site, gsc), "ga4": google_match.match_ga4(site, ga4_full)}
    return out


def _write_ads_features(
    db: Session,
    user_id: int,
    customer_id: str,
    name: str,
    campaigns: list,
    terms: list,
    cms_id: int | None = None,
) -> None:
    clicks = sum(int(float(row[3] or 0)) for row in campaigns) if campaigns else 0
    cost = sum(float(row[2] or 0) for row in campaigns) if campaigns else 0.0
    impressions = sum(int(float(row[4] or 0)) for row in campaigns) if campaigns else 0
    payload = {
        "summary": f"Live Google Ads last 30 days for {name} ({customer_id}).",
        "columns": ["Campaign", "Status", "Cost", "Clicks", "Impressions", "Conversions"],
        "rows": campaigns,
        "kpis": [
            ["Campaigns", str(len(campaigns))],
            ["Clicks", str(clicks)],
            ["Impressions", str(impressions)],
            ["Cost", f"{cost:.2f}"],
        ],
        "searchTerms": terms,
        "customerId": customer_id,
        "customerName": name,
        "cmsConnectionId": cms_id,
        "live": True,
        "source": "google-ads",
        "seedVersion": LIVE,
        "syncedAt": datetime.utcnow().isoformat(),
    }
    _upsert_feature(db, user_id, "google-ads", f"Google Ads · {name}", payload)
    _upsert_feature(
        db,
        user_id,
        "paid-search",
        "Paid Search (Google Ads)",
        {
            **payload,
            "summary": f"Search terms from Google Ads last 30 days for {name}.",
            "columns": ["Search term", "Clicks", "Impressions", "Cost"],
            "rows": terms,
        },
    )


def _persist_ads_meta(db: Session, user: User, row: GoogleConnection, ads: dict, cms_id: int | None) -> None:
    meta = dict(row.meta or {})
    meta.update({k: v for k, v in ads.items() if v})
    row.meta = meta
    flag_modified(row, "meta")
    row.updated_at = datetime.utcnow()
    db.add(row)
    if cms_id:
        cms = db.get(CmsConnection, cms_id)
        if cms is None or cms.customer_id != user.id:
            raise HTTPException(status_code=404, detail="Website not found")
        cms_meta = dict(cms.meta or {})
        cms_meta.update({k: v for k, v in ads.items() if v})
        cms.meta = cms_meta
        flag_modified(cms, "meta")
        cms.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)


@router.get("/oauth/google/ads/accounts")
def google_ads_accounts(
    cms_connection_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row is None or not (row.access_token or row.refresh_token):
        raise HTTPException(status_code=404, detail="Connect Google first")
    if not goauth.ads_scope_granted(row.scopes):
        raise HTTPException(status_code=409, detail="Google Ads permission is missing. Reconnect with Ads access.")
    if not gads.ads_configured():
        raise HTTPException(
            status_code=503,
            detail="Add GOOGLE_ADS_DEVELOPER_TOKEN to the backend .env. Create it in Google Ads → Tools → API Center.",
        )
    token = _valid_access_token(db, row)
    try:
        accounts = gads.list_ads_accounts(token)
    except Exception as exc:  # noqa: BLE001
        row.last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)[:400]) from exc
    cms = None
    if cms_connection_id:
        cms = db.get(CmsConnection, cms_connection_id)
        if cms is None or cms.customer_id != user.id:
            raise HTTPException(status_code=404, detail="Website not found")
    meta = (cms.meta if cms else row.meta) or {}
    fallback = row.meta or {}
    return {
        "configured": True,
        "accounts": accounts,
        "selectedCustomerId": meta.get("adsCustomerId") or fallback.get("adsCustomerId") or "",
        "selectedCustomerName": meta.get("adsCustomerName") or fallback.get("adsCustomerName") or "",
        "cmsConnectionId": cms.id if cms else None,
    }


@router.post("/oauth/google/ads/select")
def google_ads_select(body: AdsSelectBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Connect Google first")
    cid = re.sub(r"\D", "", body.customer_id or "")
    if not cid:
        raise HTTPException(status_code=400, detail="Choose a Google Ads customer")
    ads = {
        "adsCustomerId": cid,
        "adsCustomerName": (body.customer_name or "").strip() or f"Account {cid}",
        "adsLoginCustomerId": re.sub(r"\D", "", body.login_customer_id or "") or None,
    }
    _persist_ads_meta(db, user, row, ads, body.cms_connection_id)
    token = _valid_access_token(db, row)
    try:
        campaigns = gads.fetch_campaigns(token, cid, ads.get("adsLoginCustomerId"))
        try:
            terms = gads.fetch_search_terms(token, cid, ads.get("adsLoginCustomerId"))
        except Exception:  # noqa: BLE001
            terms = []
        _write_ads_features(db, user.id, cid, ads["adsCustomerName"], campaigns, terms, body.cms_connection_id)
    except Exception as exc:  # noqa: BLE001
        row.last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc)[:400]) from exc
    return {"ok": True, "adsCustomerId": cid, "adsCustomerName": ads["adsCustomerName"]}


@router.post("/oauth/google/ads/sync")
def google_ads_sync(body: SyncBody = SyncBody(), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Connect Google first")
    cms = None
    if body.cms_connection_id:
        cms = db.get(CmsConnection, body.cms_connection_id)
        if cms is None or cms.customer_id != user.id:
            raise HTTPException(status_code=404, detail="Website not found")
    meta = (cms.meta if cms else row.meta) or {}
    cid = re.sub(r"\D", "", str(meta.get("adsCustomerId") or (row.meta or {}).get("adsCustomerId") or ""))
    if not cid:
        raise HTTPException(status_code=400, detail="Select a Google Ads account first")
    token = _valid_access_token(db, row)
    login = meta.get("adsLoginCustomerId") or (row.meta or {}).get("adsLoginCustomerId")
    name = meta.get("adsCustomerName") or (row.meta or {}).get("adsCustomerName") or f"Account {cid}"
    campaigns = gads.fetch_campaigns(token, cid, login)
    try:
        terms = gads.fetch_search_terms(token, cid, login)
    except Exception:  # noqa: BLE001
        terms = []
    _write_ads_features(db, user.id, cid, name, campaigns, terms, body.cms_connection_id)
    return {"ok": True, "campaigns": len(campaigns), "searchTerms": len(terms)}


@router.post("/oauth/google/ads/disconnect")
def google_ads_disconnect(body: SyncBody = SyncBody(), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    keys = ("adsCustomerId", "adsCustomerName", "adsLoginCustomerId")
    if row and not body.cms_connection_id:
        meta = dict(row.meta or {})
        for key in keys:
            meta.pop(key, None)
        row.meta = meta
        flag_modified(row, "meta")
        db.add(row)
    if body.cms_connection_id:
        cms = db.get(CmsConnection, body.cms_connection_id)
        if cms and cms.customer_id == user.id:
            meta = dict(cms.meta or {})
            for key in keys:
                meta.pop(key, None)
            cms.meta = meta
            flag_modified(cms, "meta")
    for rec in db.query(FeatureRecord).filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind.in_(["google-ads", "paid-search"])).all():
        source = str((rec.payload or {}).get("source") or "")
        if source.startswith("google-ads") or source == "google-ads":
            db.delete(rec)
    db.commit()
    return {"ok": True, "connection": _connection_payload(row)}


@router.post("/oauth/google/select")
def google_select(body: SelectBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row is None or not (row.access_token or row.refresh_token):
        raise HTTPException(status_code=404, detail="Connect Google first")
    token = _valid_access_token(db, row)
    gsc_value = body.gsc_site_url.strip() if body.gsc_site_url is not None else None
    ga4_value = body.ga4_property_id.strip() if body.ga4_property_id is not None else None
    try:
        if gsc_value:
            allowed = {p["siteUrl"]: p for p in goauth.list_gsc_sites(token)}
            if gsc_value not in allowed or allowed[gsc_value].get("permissionLevel") in google_match.UNUSABLE:
                raise HTTPException(status_code=422, detail={"code": "gsc_no_access", "message": f"{row.google_email or 'This Google account'} cannot read Search Console data for {gsc_value}."})
        if ga4_value:
            props = {p["propertyId"]: p for p in goauth.list_ga4_properties(token)}
            if ga4_value not in props:
                raise HTTPException(status_code=422, detail={"code": "ga4_no_access", "message": f"{row.google_email or 'This Google account'} cannot read Analytics property {ga4_value}."})
            if body.ga4_property_name is None:
                body.ga4_property_name = props[ga4_value].get("displayName") or ""
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=_friendly_google_error(exc)[:300]) from exc
    if gsc_value is not None:
        row.gsc_site_url = gsc_value
        _stamp_validated(row, "gsc", gsc_value)
    if ga4_value is not None:
        row.ga4_property_id = ga4_value
        _stamp_validated(row, "ga4", ga4_value)
    if body.ga4_property_name is not None:
        row.ga4_property_name = body.ga4_property_name.strip()
    row.updated_at = datetime.utcnow()
    row.status = "connected"
    row.last_error = ""
    _remember_account(row)
    if body.cms_connection_id:
        cms = db.get(CmsConnection, body.cms_connection_id)
        if cms is None or cms.customer_id != user.id:
            raise HTTPException(status_code=404, detail="Website not found")
        meta = dict(cms.meta or {})
        if body.gsc_site_url is not None:
            meta["gscSiteUrl"] = body.gsc_site_url.strip()
        if body.ga4_property_id is not None:
            meta["ga4PropertyId"] = body.ga4_property_id.strip()
        if body.ga4_property_name is not None:
            meta["ga4PropertyName"] = body.ga4_property_name.strip()
        cms.meta = meta
        flag_modified(cms, "meta")
        cms.updated_at = datetime.utcnow()
    db.commit()
    return _connection_payload(row)


@router.post("/oauth/google/sync")
def google_sync(body: SyncBody = SyncBody(), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return sync_user_google(db, user.id, body.cms_connection_id)


def _apply_site_google(db: Session, user_id: int, row: GoogleConnection, cms_connection_id: int | None) -> int | None:
    if not cms_connection_id:
        sites = (
            db.query(CmsConnection)
            .filter(CmsConnection.customer_id == user_id, CmsConnection.status == "connected")
            .all()
        )
        if len(sites) == 1:
            cms_connection_id = sites[0].id
        else:
            return None
    cms = db.get(CmsConnection, cms_connection_id)
    if cms is None or cms.customer_id != user_id:
        raise HTTPException(status_code=404, detail="Website not found")
    meta = cms.meta or {}
    if meta.get("gscSiteUrl"):
        row.gsc_site_url = str(meta["gscSiteUrl"])
    if meta.get("ga4PropertyId"):
        row.ga4_property_id = str(meta["ga4PropertyId"])
        row.ga4_property_name = str(meta.get("ga4PropertyName") or row.ga4_property_name or "")
    db.info["cms_connection_id"] = cms.id
    return cms.id


def sync_user_google(db: Session, user_id: int, cms_connection_id: int | None = None) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Connect Google first")
    _apply_site_google(db, user.id, row, cms_connection_id)
    token = _valid_access_token(db, row)
    synced = {"gsc": False, "ga4": False, "pagespeed": False, "places": False, "ads": False}
    overview_rows: list[list] = []
    hub_panels: list[dict] = []
    queries: list[list] = []
    pages: list[list] = []
    countries: list[list] = []
    devices: list[list] = []
    report: dict = {}
    mobile: dict | None = None
    desktop: dict | None = None
    places: list[list] = []
    speed_url = ""
    enriched: dict = {}

    try:
        site_label = row.gsc_site_url or row.ga4_property_name or "Connected site"

        if row.gsc_site_url:
            queries = goauth.fetch_gsc_top_queries(token, row.gsc_site_url, row_limit=40)
            pages = goauth.fetch_gsc_top_pages(token, row.gsc_site_url, row_limit=30)
            countries = goauth.fetch_gsc_by_country(token, row.gsc_site_url, row_limit=15)
            devices = goauth.fetch_gsc_by_device(token, row.gsc_site_url, row_limit=10)
            daily = goauth.fetch_gsc_daily(token, row.gsc_site_url, days=28)
            weekly = goauth.gsc_weekly_clicks(daily, weeks=4)
            total_clicks = sum(int(float(r[1] or 0)) for r in daily) if daily else sum(int(float(r[1] or 0)) for r in queries)
            total_impr = sum(int(float(r[2] or 0)) for r in daily) if daily else sum(int(float(r[2] or 0)) for r in queries)
            avg_ctr = f"{(total_clicks / total_impr * 100):.2f}%" if total_impr else "—"
            avg_pos = (
                f"{sum(float(r[4] or 0) for r in queries) / len(queries):.1f}" if queries else "—"
            )
            gsc_kpis = [
                ["Clicks", str(total_clicks)],
                ["Impressions", str(total_impr)],
                ["CTR", avg_ctr],
                ["Avg position", avg_pos],
                ["Queries", str(len(queries))],
            ]
            query_trend = [[r[0][:18], int(float(r[1] or 0)), int(float(r[2] or 0))] for r in queries[:10]]
            weekly_trend = [[w[0], int(w[1])] for w in weekly]
            last_day = daily[-1][0] if daily else ""

            _upsert_feature(
                db,
                user.id,
                "organic-search",
                "Organic Search (GSC)",
                {
                    "summary": f"Live Search Console queries for {row.gsc_site_url} (last 28 days).",
                    "columns": ["Query", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": queries,
                    "kpis": gsc_kpis,
                    "daily": daily,
                    "weekly": weekly,
                    "syncedAt": datetime.utcnow().isoformat(),
                    "panels": {
                        "kpis": gsc_kpis,
                        "trend": weekly_trend or query_trend,
                        "weeklyClicks": weekly_trend,
                        "dailyClicks": [[d[0], int(float(d[1] or 0))] for d in daily],
                        "trendLabels": ["Weekly organic clicks"],
                        "countries": [[c[0], c[1], c[2]] for c in countries[:8]],
                        "devices": [[d[0], d[1]] for d in devices],
                        "pages": [[p[0][-40:], p[1]] for p in pages[:8]],
                    },
                    "concept": "gsc-queries",
                    "source": "google_search_console",
                    "siteUrl": row.gsc_site_url,
                    "lastCompleteDay": last_day,
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "keyword-research",
                "Keyword Research (GSC)",
                {
                    "summary": f"Live Search Console queries for {row.gsc_site_url}.",
                    "columns": ["Keyword", "Impressions", "Source", "Position"],
                    "rows": [[q[0], q[2] if len(q) > 2 else "—", "Search Console", q[4] if len(q) > 4 else (q[3] if len(q) > 3 else "—")] for q in queries],
                    "kpis": [["Queries", str(len(queries))], ["Clicks", str(total_clicks)]],
                    "source": "google_search_console",
                    "siteUrl": row.gsc_site_url,
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "top-pages",
                "Top Pages (GSC)",
                {
                    "summary": f"Top landing pages from Search Console for {row.gsc_site_url}.",
                    "columns": ["Page", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": pages,
                    "kpis": [
                        ["Pages", str(len(pages))],
                        ["Top clicks", pages[0][1] if pages else "0"],
                        ["Best CTR", pages[0][3] if pages else "—"],
                    ],
                    "panels": {
                        "kpis": [
                            ["Pages", str(len(pages))],
                            ["Top clicks", pages[0][1] if pages else "0"],
                        ],
                        "pages": [[p[0][-40:], p[1]] for p in pages[:10]],
                        "distribution": [[p[0][-24:], p[4]] for p in pages[:8]],
                    },
                    "concept": "gsc-pages",
                    "source": "google_search_console",
                    "siteUrl": row.gsc_site_url,
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "gsc-countries",
                "GSC by Country",
                {
                    "summary": "Search Console performance by country.",
                    "columns": ["Country", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": countries,
                    "panels": {"countries": [[c[0], c[1], c[2]] for c in countries]},
                    "concept": "gsc-geo",
                    "source": "google_search_console",
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "gsc-devices",
                "GSC by Device",
                {
                    "summary": "Search Console performance by device.",
                    "columns": ["Device", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": devices,
                    "panels": {"devices": [[d[0], d[1]] for d in devices]},
                    "concept": "gsc-device",
                    "source": "google_search_console",
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "organic-research",
                "Organic Research (GSC)",
                {
                    "summary": f"Query × clicks research view from Search Console for {row.gsc_site_url}.",
                    "columns": ["Keyword", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": queries,
                    "panels": {
                        "kpis": gsc_kpis,
                        "trend": query_trend,
                        "trendLabels": ["Clicks", "Impressions"],
                    },
                    "concept": "gsc-queries",
                    "source": "google_search_console",
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "website-keywords",
                "Website Keywords (GSC)",
                {
                    "summary": f"Site keywords from Search Console for {row.gsc_site_url}.",
                    "columns": ["Keyword", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": queries,
                    "panels": {
                        "kpis": gsc_kpis,
                        "pages": [[p[0][-40:], p[1]] for p in pages[:8]],
                    },
                    "concept": "gsc-queries",
                    "source": "google_search_console",
                    "seedVersion": LIVE,
                },
            )
            hub_panels.append(
                {
                    "title": "Search Console · top queries",
                    "columns": ["Query", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": queries[:8],
                }
            )
            hub_panels.append(
                {
                    "title": "Search Console · top pages",
                    "columns": ["Page", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": pages[:8],
                }
            )
            # Position tracking snapshot from GSC avg positions
            today = datetime.utcnow().date().isoformat()
            snap_rows = [[today, q[0], q[4], q[1], q[2]] for q in queries[:25]]
            prev = (
                db.query(FeatureRecord)
                .filter(
                    FeatureRecord.customer_id == user.id,
                    FeatureRecord.kind == "position-tracking",
                    FeatureRecord.title == "Position Tracking (GSC)",
                )
                .order_by(FeatureRecord.id.desc())
                .first()
            )
            history = []
            if prev and (prev.payload or {}).get("seedVersion") == LIVE:
                history = list((prev.payload or {}).get("rows") or [])
                # drop same-day duplicates
                history = [r for r in history if not (r and r[0] == today)]
            history = (snap_rows + history)[:200]
            _upsert_feature(
                db,
                user.id,
                "position-tracking",
                "Position Tracking (GSC)",
                {
                    "summary": "Average position history from Search Console queries (owned queries, not full SERP rivals).",
                    "columns": ["Date", "Query", "Position", "Clicks", "Impressions"],
                    "rows": history,
                    "kpis": [
                        ["Tracked", str(len(snap_rows))],
                        ["History rows", str(len(history))],
                        ["As of", today],
                    ],
                    "panels": {
                        "kpis": [["Tracked", str(len(snap_rows))], ["History", str(len(history))]],
                        "trend": [[r[1][:14], float(r[2] or 0)] for r in snap_rows[:10]],
                        "trendLabels": ["Avg position"],
                    },
                    "concept": "ranks",
                    "source": "google_search_console",
                    "seedVersion": LIVE,
                },
            )
            synced["gsc"] = True
            _stamp_validated(row, "gsc", row.gsc_site_url)

        if row.ga4_property_id:
            report = goauth.fetch_ga4_overview(token, row.ga4_property_id)
            daily = report.get("daily") or []
            channels = report.get("channels") or []
            landings = report.get("landingPages") or []
            referral = report.get("referral") or []
            ga4_kpis = [
                ["Sessions", str(report["sessions"])],
                ["Users", str(report["users"])],
                ["Views", str(report["views"])],
                ["Channels", str(len(channels))],
            ]
            _upsert_feature(
                db,
                user.id,
                "traffic-analytics",
                "Traffic Analytics (GA4)",
                {
                    "summary": f"Live GA4 traffic for {row.ga4_property_name or row.ga4_property_id} (last 28 days).",
                    "columns": ["Date", "Sessions", "Users"],
                    "rows": daily,
                    "kpis": ga4_kpis,
                    "panels": {
                        "kpis": ga4_kpis,
                        "trend": [[d[0][5:], int(float(d[1] or 0)), int(float(d[2] or 0))] for d in daily[-14:]],
                        "trendLabels": ["Sessions", "Users"],
                        "distribution": [[c[0], c[1]] for c in channels],
                    },
                    "concept": "ga4-overview",
                    "source": "google_analytics_4",
                    "propertyId": row.ga4_property_id,
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "traffic-home",
                "Research Traffic (GA4)",
                {
                    "summary": "Daily GA4 sessions for the connected property.",
                    "columns": ["Date", "Sessions", "Users"],
                    "rows": daily,
                    "panels": {
                        "kpis": ga4_kpis[:3],
                        "trend": [[d[0][5:], int(float(d[1] or 0))] for d in daily[-14:]],
                        "trendLabels": ["Sessions"],
                    },
                    "concept": "ga4-overview",
                    "source": "google_analytics_4",
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "traffic-distribution",
                "Traffic Distribution (GA4)",
                {
                    "summary": "Channel mix from Google Analytics 4.",
                    "columns": ["Channel", "Sessions", "Users", "Views"],
                    "rows": channels,
                    "panels": {
                        "kpis": ga4_kpis,
                        "distribution": [[c[0], c[1]] for c in channels],
                    },
                    "concept": "ga4-channels",
                    "source": "google_analytics_4",
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "ga4-landing-pages",
                "GA4 Landing Pages",
                {
                    "summary": "Top landing pages from GA4.",
                    "columns": ["Landing page", "Sessions", "Users", "Bounce rate"],
                    "rows": landings,
                    "panels": {
                        "pages": [[(p[0] or "")[-40:], p[1]] for p in landings[:10]],
                        "kpis": [
                            ["Landings", str(len(landings))],
                            ["Top sessions", landings[0][1] if landings else "0"],
                        ],
                    },
                    "concept": "ga4-landings",
                    "source": "google_analytics_4",
                    "seedVersion": LIVE,
                },
            )
            _upsert_feature(
                db,
                user.id,
                "referral",
                "Referral (GA4)",
                {
                    "summary": "Referral / source traffic from GA4.",
                    "columns": ["Source / medium", "Sessions", "Users", "Views"],
                    "rows": referral,
                    "panels": {
                        "distribution": [[r[0][:36], r[1]] for r in referral[:10]],
                        "kpis": [["Sources", str(len(referral))], ["Top sessions", referral[0][1] if referral else "0"]],
                    },
                    "concept": "ga4-referral",
                    "source": "google_analytics_4",
                    "seedVersion": LIVE,
                },
            )
            ai_traffic = report.get("aiTraffic") or []
            _upsert_feature(
                db,
                user.id,
                "ai-traffic",
                "AI Traffic (GA4)",
                {
                    "summary": "Sessions from AI product referrers in GA4 (ChatGPT, Perplexity, Gemini, etc.).",
                    "columns": ["Source / medium", "Sessions", "Users", "Views"],
                    "rows": ai_traffic
                    or [["No AI referrers in last 28 days", "0", "0", "0"]],
                    "kpis": [
                        ["AI sources", str(len(ai_traffic))],
                        ["Sessions", str(sum(int(float(r[1] or 0)) for r in ai_traffic))],
                    ],
                    "panels": {
                        "distribution": [[r[0][:36], r[1]] for r in ai_traffic[:10]],
                        "kpis": [["AI sources", str(len(ai_traffic))]],
                    },
                    "concept": "ga4-referral",
                    "source": "google_analytics_4",
                    "seedVersion": LIVE,
                },
            )
            # Join GSC top pages with GA4 landings when both exist
            if pages and landings:
                def _norm(path: str) -> str:
                    p = (path or "").strip()
                    p = p.replace("https://", "").replace("http://", "")
                    if "/" in p:
                        p = "/" + "/".join(p.split("/")[1:])
                    return p.rstrip("/") or "/"

                ga4_map = {_norm(r[0]): r for r in landings}
                joined = []
                for page in pages:
                    key = _norm(page[0])
                    ga = ga4_map.get(key) or ga4_map.get(key.split("?")[0])
                    joined.append(
                        [
                            page[0],
                            page[1],
                            page[2],
                            page[3],
                            page[4],
                            ga[1] if ga else "—",
                            ga[2] if ga else "—",
                        ]
                    )
                _upsert_feature(
                    db,
                    user.id,
                    "top-pages",
                    "Top Pages (GSC + GA4)",
                    {
                        "summary": f"GSC pages joined with GA4 landing sessions for {row.gsc_site_url}.",
                        "columns": ["Page", "GSC Clicks", "GSC Impr", "CTR", "Position", "GA4 Sessions", "GA4 Users"],
                        "rows": joined,
                        "kpis": [
                            ["Pages", str(len(joined))],
                            ["Joined", str(sum(1 for r in joined if r[5] != "—"))],
                        ],
                        "panels": {
                            "pages": [[(p[0] or "")[-40:], p[1]] for p in joined[:10]],
                            "kpis": [["Pages", str(len(joined))], ["With GA4", str(sum(1 for r in joined if r[5] != "—"))]],
                        },
                        "concept": "gsc-pages",
                        "source": "google_gsc_ga4_join",
                        "seedVersion": LIVE,
                    },
                )
            hub_panels.append(
                {
                    "title": "GA4 · channels",
                    "columns": ["Channel", "Sessions", "Users", "Views"],
                    "rows": channels[:8],
                }
            )
            hub_panels.append(
                {
                    "title": "GA4 · landing pages",
                    "columns": ["Landing page", "Sessions", "Users", "Bounce rate"],
                    "rows": landings[:8],
                }
            )
            synced["ga4"] = True
            _stamp_validated(row, "ga4", row.ga4_property_id)

        # PageSpeed on connected site (API key) — full PSI report + GSC findings
        speed_url = row.gsc_site_url or ""
        if speed_url.startswith("sc-domain:"):
            speed_url = f"https://{speed_url.replace('sc-domain:', '', 1)}/"
        if speed_url and config.GOOGLE_PAGESPEED_API_KEY:
            try:
                mobile = goauth.run_pagespeed(speed_url, "mobile")
                desktop = goauth.run_pagespeed(speed_url, "desktop")
                audit_payload = goauth.build_site_audit_payload(
                    url=speed_url,
                    mobile=mobile,
                    desktop=desktop,
                    gsc_pages=pages,
                    gsc_queries=queries,
                )
                _upsert_feature(db, user.id, "site-audit", "Site Audit (PageSpeed + GSC)", audit_payload)
                _upsert_feature(
                    db,
                    user.id,
                    "on-page-seo",
                    "On-page / PageSpeed SEO",
                    {
                        "summary": f"SEO & accessibility audits for {mobile.get('finalUrl') or speed_url}.",
                        "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
                        "rows": (mobile.get("seoAudits") or []) + (mobile.get("a11yAudits") or [])[:15],
                        "kpis": [
                            ["SEO", mobile["scores"]["seo"]],
                            ["A11y", mobile["scores"]["accessibility"]],
                            ["BP", mobile["scores"]["bestPractices"]],
                        ],
                        "panels": {
                            "score": int(float(mobile["scores"]["seo"] or 0)),
                            "scoreLabel": "SEO score",
                            "audit": [
                                ["Accessibility", mobile["scores"]["accessibility"]],
                                ["Best practices", mobile["scores"]["bestPractices"]],
                                ["Performance", mobile["scores"]["performance"]],
                            ],
                        },
                        "tablePanels": [
                            {
                                "title": "SEO audits",
                                "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
                                "rows": mobile.get("seoAudits") or [],
                            },
                            {
                                "title": "Accessibility audits",
                                "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
                                "rows": mobile.get("a11yAudits") or [],
                            },
                            {
                                "title": "Best practices",
                                "columns": ["Audit", "Status", "Value", "Weight", "Detail"],
                                "rows": mobile.get("bestPracticeAudits") or [],
                            },
                        ],
                        "concept": "psi-report",
                        "source": "google_pagespeed",
                        "seedVersion": LIVE,
                    },
                )
                hub_panels.append(
                    {
                        "title": "PageSpeed Insights",
                        "columns": ["Item", "Mobile", "Desktop", "Note"],
                        "rows": (audit_payload.get("rows") or [])[:8],
                    }
                )
                synced["pagespeed"] = True
            except Exception as speed_exc:  # noqa: BLE001
                overview_rows.append([site_label, "PageSpeed", "Error", str(speed_exc)[:120]])

        # Places — fill all Local tool kinds from live Places + derived workflows
        if config.GOOGLE_PLACES_API_KEY:
            try:
                host = site_label.replace("https://", "").replace("http://", "").replace("sc-domain:", "").split("/")[0]
                brand = host.split(".")[0] if host else "seo agency"
                places = goauth.places_text_search(f"{brand} near me", max_results=10)
                if not places:
                    places = goauth.places_text_search(f"marketing agency {brand}", max_results=10)
                avg_rating = "—"
                if places:
                    ratings = [float(p[2]) for p in places if p[2] not in {"—", "", None}]
                    if ratings:
                        avg_rating = f"{sum(ratings) / len(ratings):.1f}"
                top_name = places[0][0] if places else "—"
                top_reviews = places[0][3] if places else "0"
                _upsert_feature(
                    db,
                    user.id,
                    "local-competitors",
                    "Local competitors (Places)",
                    {
                        "summary": f"Google Places results related to {brand}.",
                        "columns": ["Name", "Address", "Rating", "Reviews", "Website"],
                        "rows": places,
                        "kpis": [
                            ["Places", str(len(places))],
                            ["Avg rating", avg_rating],
                            ["Top reviews", top_reviews],
                        ],
                        "panels": {
                            "kpis": [["Places", str(len(places))], ["Avg rating", avg_rating]],
                            "distribution": [[p[0][:28], p[2]] for p in places[:8]],
                        },
                        "concept": "places",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                _upsert_feature(
                    db,
                    user.id,
                    "local-dashboard",
                    "Local dashboard (Places)",
                    {
                        "summary": f"Local visibility snapshot for {brand} from Places + sync.",
                        "columns": ["Metric", "Value", "Goal"],
                        "rows": [
                            ["Nearby competitors", str(len(places)), "Track top 10"],
                            ["Avg. rating (area)", avg_rating, "≥ 4.5"],
                            ["Top competitor", top_name[:40], avg_rating],
                            ["Top reviews", top_reviews, "Monitor"],
                            ["Brand query", brand, host],
                            ["GSC site", row.gsc_site_url or "—", "Connected" if synced["gsc"] else "Connect"],
                            ["Last Places sync", datetime.utcnow().isoformat(timespec="seconds"), "Auto"],
                        ],
                        "panels": {
                            "kpis": [
                                ["Places", str(len(places))],
                                ["Avg rating", avg_rating],
                                ["Top", top_name[:18] if top_name != "—" else "—"],
                            ],
                            "score": int(float(avg_rating) * 20) if avg_rating != "—" else 0,
                            "scoreLabel": "Local score",
                        },
                        "concept": "google-dash",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                listing_rows = [
                    [place[0], place[1] or "—", place[2], "Google Places", place[3]]
                    for place in places
                ]
                _upsert_feature(
                    db,
                    user.id,
                    "local-listings",
                    "Listing Management",
                    {
                        "summary": f"Live Places listings for {brand}." if places else "No Places results yet.",
                        "columns": ["Place", "Address", "Rating", "Source", "Reviews"],
                        "rows": listing_rows,
                        "kpis": [["Live Places", str(len(places))]],
                        "concept": "pipeline",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                review_rows = [[place[0][:48], place[3], place[2], "Google Places"] for place in places[:8]]
                _upsert_feature(
                    db,
                    user.id,
                    "local-reviews",
                    "Review Management",
                    {
                        "summary": f"Review counts from Places for {brand}.",
                        "columns": ["Place", "Reviews", "Rating", "Source"],
                        "rows": review_rows,
                        "kpis": [["Tracked", str(len(places))], ["Avg rating", avg_rating]],
                        "concept": "pipeline",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                _upsert_feature(
                    db,
                    user.id,
                    "local-gbp",
                    "GBP Optimization",
                    {
                        "summary": f"Facts from the latest Places sync for {brand}.",
                        "columns": ["Check", "Status", "Detail"],
                        "rows": [
                            ["Places synced", "Complete" if places else "Needs sync", f"{len(places)} listings"],
                            ["Website link", "Complete" if row.gsc_site_url else "Needs Google connect", row.gsc_site_url or "—"],
                            ["Top listing", "Live" if places else "Waiting", f"{top_name} · {top_reviews} reviews"],
                        ],
                        "concept": "checklist",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                _upsert_feature(
                    db,
                    user.id,
                    "local-automations",
                    "Automations",
                    {
                        "summary": "Local refresh follows Google Sync. No scheduled dummy jobs.",
                        "columns": ["Task", "Status", "Source"],
                        "rows": [
                            ["Google Sync (GSC/GA4/PSI/Places)", "Manual / on demand", "Searchify"],
                            ["Places refresh", "With Sync" if places else "Waiting for sync", "Places API"],
                        ],
                        "concept": "pipeline",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                _upsert_feature(
                    db,
                    user.id,
                    "local-ai-agent",
                    "GBP AI Agent",
                    {
                        "summary": "Draft replies only after you ask. Nothing is invented or posted.",
                        "columns": ["Review", "Suggestion", "Status", "Rating", "Source"],
                        "rows": [],
                        "kpis": [["Drafts", "0"]],
                        "concept": "pipeline",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                map_rows = [[place[0], str(idx + 1), place[2], place[3]] for idx, place in enumerate(places[:8])]
                _upsert_feature(
                    db,
                    user.id,
                    "local-map-ranks",
                    "Map Rank Tracker",
                    {
                        "summary": f"Places result order for {brand} (not a paid rank grid).",
                        "columns": ["Place", "Order", "Rating", "Reviews"],
                        "rows": map_rows,
                        "concept": "ranks",
                        "source": "google_places",
                        "seedVersion": LIVE,
                    },
                )
                hub_panels.append(
                    {
                        "title": "Places · local results",
                        "columns": ["Name", "Address", "Rating", "Reviews", "Website"],
                        "rows": places[:6],
                    }
                )
                synced["places"] = True
            except Exception as places_exc:  # noqa: BLE001
                overview_rows.append([site_label, "Places", "Error", str(places_exc)[:120]])

        fresh = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user_id).first()
        if fresh is None:
            raise HTTPException(status_code=404, detail="Google was disconnected during sync. Connect Google again.")
        row = fresh
        ads_meta = dict(row.meta or {})
        cms_id = db.info.get("cms_connection_id")
        if cms_id:
            cms_row = db.get(CmsConnection, cms_id)
            if cms_row and cms_row.meta:
                ads_meta = {**ads_meta, **cms_row.meta}
        ads_cid = re.sub(r"\D", "", str(ads_meta.get("adsCustomerId") or ""))
        if ads_cid and goauth.ads_scope_granted(row.scopes) and gads.ads_configured():
            try:
                ads_name = ads_meta.get("adsCustomerName") or f"Account {ads_cid}"
                campaigns = gads.fetch_campaigns(token, ads_cid, ads_meta.get("adsLoginCustomerId"))
                try:
                    terms = gads.fetch_search_terms(token, ads_cid, ads_meta.get("adsLoginCustomerId"))
                except Exception:  # noqa: BLE001
                    terms = []
                _write_ads_features(db, user.id, ads_cid, ads_name, campaigns, terms, cms_id)
                hub_panels.append(
                    {
                        "title": "Google Ads · campaigns",
                        "columns": ["Campaign", "Status", "Cost", "Clicks", "Impressions", "Conversions"],
                        "rows": campaigns[:8],
                    }
                )
                synced["ads"] = True
            except Exception as ads_exc:  # noqa: BLE001
                overview_rows.append([site_label, "Google Ads", "Error", str(ads_exc)[:120]])

        # Composite dashboards from whatever synced
        dash_rows = [
            ["Search Console", "Connected" if synced["gsc"] else "—", str(len(queries)), "queries"],
            ["GA4 sessions", "Connected" if synced["ga4"] else "—", str(report.get("sessions", "—")), "28d"],
            ["PageSpeed mobile", "Synced" if synced["pagespeed"] else "—", (mobile or {}).get("scores", {}).get("performance", "—"), speed_url or "—"],
            ["Places", "Synced" if synced["places"] else "—", str(len(places)), "nearby"],
        ]
        dash_panels: dict = {"kpis": [[r[0], r[2]] for r in dash_rows]}
        if queries:
            dash_panels["trend"] = [[r[0][:14], int(float(r[1] or 0))] for r in queries[:10]]
            dash_panels["trendLabels"] = ["GSC clicks"]
        if countries:
            dash_panels["countries"] = [[c[0], c[1], c[2]] for c in countries[:6]]
        if devices:
            dash_panels["devices"] = [[d[0], d[1]] for d in devices]
        if mobile:
            dash_panels["score"] = int(float(mobile["scores"]["performance"] or 0))
            dash_panels["scoreLabel"] = "Mobile PageSpeed"
        _upsert_feature(
            db,
            user.id,
            "seo-dashboard",
            "SEO Dashboard (Google)",
            {
                "summary": f"Live Google composite for {site_label}.",
                "columns": ["Widget", "Status", "Value", "Detail"],
                "rows": dash_rows,
                "panels": dash_panels,
                "concept": "google-dash",
                "source": "google_composite",
                "seedVersion": LIVE,
            },
        )
        _upsert_feature(
            db,
            user.id,
            "visibility-overview",
            "Visibility Overview (Google)",
            {
                "summary": "Search Console + GA4 visibility snapshot.",
                "columns": ["Signal", "Value", "Source"],
                "rows": [
                    ["GSC queries", str(len(queries)), "Search Console"],
                    ["GSC top clicks", queries[0][1] if queries else "0", "Search Console"],
                    ["GA4 sessions", str(report.get("sessions", "—")), "GA4"],
                    ["GA4 users", str(report.get("users", "—")), "GA4"],
                    ["Mobile perf", (mobile or {}).get("scores", {}).get("performance", "—"), "PageSpeed"],
                ],
                "panels": dash_panels,
                "concept": "google-dash",
                "source": "google_composite",
                "seedVersion": LIVE,
            },
        )
        _upsert_feature(
            db,
            user.id,
            "domain-snapshot",
            "Domain Snapshot (Google)",
            {
                "summary": f"Owned-site snapshot for {site_label} from connected Google APIs.",
                "columns": ["Metric", "Value", "Source"],
                "rows": [
                    ["Site", site_label, "Connection"],
                    ["Organic queries", str(len(queries)), "GSC"],
                    ["Top page clicks", pages[0][1] if pages else "0", "GSC"],
                    ["Sessions 28d", str(report.get("sessions", "—")), "GA4"],
                    ["PageSpeed", (mobile or {}).get("scores", {}).get("performance", "—"), "PSI"],
                    ["Local places", str(len(places)), "Places"],
                ],
                "panels": dash_panels,
                "concept": "google-dash",
                "source": "google_composite",
                "seedVersion": LIVE,
            },
        )

        row.last_sync_at = datetime.utcnow()
        row.last_error = ""
        row.status = "connected"

        overview_rows = [
            [site_label, "Search Console", "Connected" if synced["gsc"] else "Not synced", row.gsc_site_url or "—"],
            [site_label, "GA4", "Connected" if synced["ga4"] else "Not synced", row.ga4_property_name or row.ga4_property_id or "—"],
            [site_label, "PageSpeed", "Synced" if synced["pagespeed"] else "Skipped/error", speed_url or "—"],
            [site_label, "Places", "Synced" if synced["places"] else "Skipped/error", "Local competitors"],
            [site_label, "Google Ads", "Connected" if synced["ads"] else "Not synced", (row.meta or {}).get("adsCustomerName") or (row.meta or {}).get("adsCustomerId") or "—"],
            [site_label, "Google account", "Connected", row.google_email or "—"],
            [site_label, "Last sync", "Done", row.last_sync_at.isoformat()],
        ] + overview_rows

        _upsert_feature(
            db,
            user.id,
            "get-started",
            "Google connections",
            {
                "summary": f"Connected {row.google_email}. Live Google services synced.",
                "columns": ["Site", "Step", "Status", "Detail"],
                "rows": overview_rows,
                "source": "google_oauth",
                "seedVersion": LIVE,
            },
        )
        _upsert_feature(
            db,
            user.id,
            "google-services",
            "Google services hub",
            {
                "summary": f"Unified Google data hub for {site_label}.",
                "columns": ["Service", "Status", "Detail", "Updated"],
                "rows": [
                    ["Search Console", "Connected" if synced["gsc"] else "—", row.gsc_site_url or "—", row.last_sync_at.isoformat()],
                    ["GA4", "Connected" if synced["ga4"] else "—", row.ga4_property_name or "—", row.last_sync_at.isoformat()],
                    ["PageSpeed", "Synced" if synced["pagespeed"] else "—", speed_url or "—", row.last_sync_at.isoformat()],
                    ["Places", "Synced" if synced["places"] else "—", "Local competitors", row.last_sync_at.isoformat()],
                    ["Google Ads", "Connected" if synced["ads"] else "—", (row.meta or {}).get("adsCustomerName") or "—", row.last_sync_at.isoformat()],
                ],
                "panels": hub_panels,
                "kpis": [
                    ["GSC", "On" if synced["gsc"] else "Off"],
                    ["GA4", "On" if synced["ga4"] else "Off"],
                    ["PageSpeed", "On" if synced["pagespeed"] else "Off"],
                    ["Places", "On" if synced["places"] else "Off"],
                ],
                "concept": "hub",
                "source": "google_services_hub",
                "seedVersion": LIVE,
            },
        )

        enriched = {}
        try:
            from app.enrichment import enrich_from_google

            enriched = enrich_from_google(db, user.id)
        except Exception as enrich_exc:  # noqa: BLE001
            enriched = {"error": str(enrich_exc)[:200]}

        db.add(
            Job(
                customer_id=user.id,
                kind="sync",
                status="done",
                detail={"provider": "google", "synced": synced, "enriched": enriched},
            )
        )
        try:
            from app.routers.product import bump_usage

            bump_usage(
                db,
                user.id,
                gsc=1 if synced.get("gsc") else 0,
                ga4=1 if synced.get("ga4") else 0,
                pagespeed=1 if synced.get("pagespeed") else 0,
                places=1 if synced.get("places") else 0,
                ads=1 if synced.get("ads") else 0,
                openai=1 if enriched and not enriched.get("error") else 0,
            )
        except Exception:  # noqa: BLE001
            pass
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        detail = _friendly_google_error(exc)
        fresh = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user_id).first()
        if fresh is not None:
            fresh.last_error = detail[:500]
            fresh.status = "error"
            db.commit()
        raise HTTPException(status_code=502, detail=detail[:300]) from exc

    return {"ok": True, "synced": synced, "enriched": enriched, "connection": _connection_payload(row)}


@router.post("/oauth/google/enrich")
def google_enrich(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Re-run OpenAI enrichment from already-synced GSC/GA4 rows."""
    from app.enrichment import enrich_from_google

    try:
        result = enrich_from_google(db, user.id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    return {"ok": True, "enriched": result}


@router.post("/oauth/google/pagespeed")
def google_pagespeed(body: PageSpeedBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not config.GOOGLE_PAGESPEED_API_KEY:
        raise HTTPException(status_code=503, detail="GOOGLE_PAGESPEED_API_KEY is not set")
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    url = (body.url or "").strip()
    if not url and row and row.gsc_site_url:
        url = row.gsc_site_url
        if url.startswith("sc-domain:"):
            url = f"https://{url.replace('sc-domain:', '', 1)}/"
    if not url:
        raise HTTPException(status_code=400, detail="Provide a URL or connect GSC first")
    try:
        mobile = goauth.run_pagespeed(url, body.strategy or "mobile")
        desktop = None
        if (body.strategy or "mobile") == "mobile":
            try:
                desktop = goauth.run_pagespeed(url, "desktop")
            except Exception:  # noqa: BLE001
                desktop = None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc

    # Attach recent GSC rows if available
    gsc_pages = gsc_queries = None
    page_rec = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "top-pages")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    query_rec = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user.id, FeatureRecord.kind == "organic-search")
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if page_rec and (page_rec.payload or {}).get("rows"):
        gsc_pages = page_rec.payload["rows"]
    if query_rec and (query_rec.payload or {}).get("rows"):
        gsc_queries = query_rec.payload["rows"]

    payload = goauth.build_site_audit_payload(
        url=url,
        mobile=mobile,
        desktop=desktop,
        gsc_pages=gsc_pages,
        gsc_queries=gsc_queries,
    )
    _upsert_feature(db, user.id, "site-audit", "Site Audit (PageSpeed + GSC)", payload)
    return {"ok": True, "result": mobile, "desktop": desktop, "payload": {"kpis": payload.get("kpis"), "counts": payload.get("counts")}}


@router.post("/oauth/google/places")
def google_places(body: PlacesBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not config.GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=503, detail="GOOGLE_PLACES_API_KEY is not set")
    query = (body.query or "").strip()
    if not query:
        row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
        host = (row.gsc_site_url if row else "") or "local business"
        host = host.replace("https://", "").replace("http://", "").replace("sc-domain:", "").split("/")[0]
        query = f"{host.split('.')[0]} competitors"
    try:
        places = goauth.places_text_search(query, max_results=min(max(body.max_results, 1), 20))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    _upsert_feature(
        db,
        user.id,
        "local-competitors",
        "Local competitors (Places)",
        {
            "summary": f"Google Places search: {query}",
            "columns": ["Name", "Address", "Rating", "Reviews", "Website"],
            "rows": places,
            "source": "google_places",
            "seedVersion": LIVE,
            "query": query,
        },
    )
    return {"ok": True, "query": query, "count": len(places), "rows": places}


@router.post("/oauth/google/auto-sync")
def google_auto_sync_now(user: User = Depends(get_current_user)):
    """Run due auto-syncs immediately (admin/client trigger)."""
    from app.auto_sync import run_due_syncs

    _ = user
    return {"ok": True, **run_due_syncs()}


@router.post("/oauth/google/accounts/use")
def use_google_account(body: GoogleAccountBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    email = body.email.strip().lower()
    if row is None or not email:
        raise HTTPException(status_code=404, detail="Connect a Google account first")
    if (row.google_email or "").lower() == email and (row.access_token or row.refresh_token):
        return {"ok": True, "connection": _connection_payload(row)}
    saved = ((row.meta or {}).get("accounts") or {}).get(email)
    if not isinstance(saved, dict) or not (saved.get("refreshToken") or saved.get("accessToken")):
        raise HTTPException(status_code=404, detail="That Google account is not saved yet. Add it from Manage workspace.")
    _remember_account(row)
    _apply_saved_account(row, saved)
    _remember_account(row)
    db.commit()
    return {"ok": True, "connection": _connection_payload(row)}


@router.post("/oauth/google/accounts/remove")
def remove_google_account(body: GoogleAccountBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Drop one saved Google sign-in. Other websites can keep a different account. Cached reports stay."""
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    email = body.email.strip().lower()
    if row is None or not email:
        raise HTTPException(status_code=404, detail="No Google account to remove")
    meta = dict(row.meta or {})
    accounts = dict(meta.get("accounts") or {})
    accounts.pop(email, None)
    meta["accounts"] = accounts
    row.meta = meta
    flag_modified(row, "meta")
    if (row.google_email or "").lower() == email:
        replacement = next((item for item in accounts.values() if isinstance(item, dict) and (item.get("refreshToken") or item.get("accessToken"))), None)
        if replacement:
            _apply_saved_account(row, replacement)
            _remember_account(row)
        else:
            row.access_token = ""
            row.refresh_token = ""
            row.token_expiry = None
            row.google_email = ""
            row.gsc_site_url = ""
            row.ga4_property_id = ""
            row.ga4_property_name = ""
            row.status = "disconnected"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "connection": _connection_payload(row)}


@router.post("/oauth/google/disconnect")
def google_disconnect(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Remove Google OAuth and wipe cached GSC/GA4/PSI/Places feature rows for this user."""
    row = db.query(GoogleConnection).filter(GoogleConnection.customer_id == user.id).first()
    if row:
        db.delete(row)

    feature_rows = db.query(FeatureRecord).filter(FeatureRecord.customer_id == user.id).all()
    removed = 0
    for fr in feature_rows:
        payload = fr.payload or {}
        source = str(payload.get("source") or "")
        seed = str(payload.get("seedVersion") or "")
        if seed == LIVE or source.startswith("google") or source.startswith("places"):
            db.delete(fr)
            removed += 1

    queue_cleared = 0
    for change in db.query(SiteChange).filter(SiteChange.customer_id == user.id).all():
        db.delete(change)
        queue_cleared += 1

    db.commit()
    return {
        "ok": True,
        "connection": _connection_payload(None),
        "featuresCleared": removed,
        "queueCleared": queue_cleared,
    }
