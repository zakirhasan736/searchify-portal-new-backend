"""Free public site preview. Reads a few live pages and colour-codes them. No account, no paid research."""

from __future__ import annotations

from time import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.site_scan import host_of, preview_scan, public_host

router = APIRouter(prefix="/api/v1/public", tags=["public"])

WINDOW = 3600
MAX_SCANS = 6
CACHE_TTL = 900
_hits: dict[str, list[float]] = {}
_cache: dict[str, tuple[float, dict]] = {}


class PreviewBody(BaseModel):
    site: str = Field(min_length=3, max_length=300)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or "local"
    return request.client.host if request.client else "local"


def _normalize(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = f"https://{text.lstrip('/')}"
    return text.split("#", 1)[0].split("?", 1)[0]


def _allow(ip: str) -> bool:
    now = time()
    recent = [stamp for stamp in _hits.get(ip, []) if now - stamp < WINDOW]
    if len(recent) >= MAX_SCANS:
        _hits[ip] = recent
        return False
    recent.append(now)
    _hits[ip] = recent
    return True


@router.post("/site-scan")
def public_site_scan(body: PreviewBody, request: Request):
    home = _normalize(body.site)
    if not home or not public_host(home):
        raise HTTPException(status_code=400, detail="Enter a public website address, like example.com.")
    host = host_of(home)
    cached = _cache.get(host)
    now = time()
    if cached and now - cached[0] < CACHE_TTL:
        return {"ok": True, "live": True, "cached": True, "scan": cached[1]}
    if not _allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="The free scan is busy for this network. Try again in a little while.")
    scan = preview_scan(home, limit=8)
    if scan.get("error") and not scan.get("pagesRead"):
        raise HTTPException(status_code=422, detail=scan["error"])
    _cache[host] = (now, scan)
    return {"ok": True, "live": True, "cached": False, "scan": scan}
