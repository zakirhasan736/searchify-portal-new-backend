from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CrawlSnapshot, Draft, FeatureRecord, Job, User
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/crawl", tags=["crawl"])


class CrawlBody(BaseModel):
    link: str


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts: list[str] = []
        self.in_title = False
        self.description = ""
        self.anchors: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and attr.get("name", "").lower() == "description":
            self.description = attr.get("content", "")
        if tag == "a" and attr.get("href"):
            self._href = attr["href"]
            self._text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "a" and self._href:
            self.anchors.append((self._href, " ".join("".join(self._text).split())))
            self._href = ""
            self._text = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self._href:
            self._text.append(data)


def invalid(message: str = "invalid url") -> dict:
    return {"current": {"title": "", "description": "", "error": message}, "internalResources": [], "findings": []}


def run_crawl(db: Session, user: User, link: str) -> dict:
    raw = (link or "").strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.netloc or " " in parsed.netloc:
        return invalid()

    try:
        response = httpx.get(raw, follow_redirects=True, timeout=12.0, headers={"User-Agent": "SearchifyCrawler/1.0"})
        response.raise_for_status()
    except httpx.HTTPError:
        return invalid()

    parser = LinkParser()
    parser.feed(response.text)
    title = " ".join("".join(parser.title_parts).split())
    final = str(response.url)
    host = urlparse(final).netloc
    seen = set()
    links = []
    for href, text in parser.anchors:
        absolute = urljoin(final, href)
        target = urlparse(absolute)
        if target.netloc != host or target.scheme not in {"http", "https"}:
            continue
        clean = absolute.split("#", 1)[0]
        if clean in seen or clean == final:
            continue
        seen.add(clean)
        links.append({"url": clean, "title": text or clean})
        if len(links) >= 30:
            break

    findings = []
    if not title:
        findings.append(["Title tag", "Fail", "Missing <title>"])
    elif len(title) < 15:
        findings.append(["Title tag", "Warn", f"Short title ({len(title)} chars)"])
    elif len(title) > 65:
        findings.append(["Title tag", "Warn", f"Long title ({len(title)} chars)"])
    else:
        findings.append(["Title tag", "Pass", title[:80]])
    desc = parser.description or ""
    if not desc:
        findings.append(["Meta description", "Fail", "Missing meta description"])
    elif len(desc) < 50:
        findings.append(["Meta description", "Warn", f"Short description ({len(desc)} chars)"])
    elif len(desc) > 160:
        findings.append(["Meta description", "Warn", f"Long description ({len(desc)} chars)"])
    else:
        findings.append(["Meta description", "Pass", desc[:100]])
    findings.append(["Internal links found", "Info", str(len(links))])
    findings.append(["HTTP status", "Pass" if response.status_code < 400 else "Fail", str(response.status_code)])

    payload = {
        "current": {
            "title": title,
            "description": parser.description,
            "error": None,
            "url": final,
            "status": response.status_code,
        },
        "internalResources": links,
        "findings": findings,
    }

    onpage = {
        "summary": f"Own crawl checks for {final}.",
        "columns": ["Check", "Status", "Detail"],
        "rows": findings,
        "kpis": [["Findings", str(len(findings))], ["Links", str(len(links))]],
        "crawl": payload,
        "source": "own_crawl",
        "seedVersion": "google-live-v1",
        "url": final,
    }
    existing = (
        db.query(FeatureRecord)
        .filter(
            FeatureRecord.customer_id == user.id,
            FeatureRecord.kind == "on-page-seo",
            FeatureRecord.title == "On Page SEO (crawl)",
        )
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if existing:
        existing.payload = onpage
    else:
        db.add(
            FeatureRecord(
                customer_id=user.id,
                kind="on-page-seo",
                title="On Page SEO (crawl)",
                payload=onpage,
                status="stored",
            )
        )

    db.add(CrawlSnapshot(customer_id=user.id, url=final, payload=payload))
    db.commit()
    return payload


@router.post("")
def crawl(body: CrawlBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return run_crawl(db, user, body.link)


@router.post("/on-page-fix")
def crawl_with_fix_draft(body: CrawlBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Crawl URL then ask OpenAI for a fix draft (On Page SEO Checker)."""
    from app.jev import score_draft
    from app.openai_client import generate_draft_body, openai_configured

    crawl_result = run_crawl(db, user, body.link)
    findings = crawl_result.get("findings") or []
    if not openai_configured():
        return {"crawl": crawl_result, "draft": None, "detail": "OPENAI_API_KEY not set"}
    brief = (
        f"URL: {crawl_result.get('current', {}).get('url')}\n"
        f"Title: {crawl_result.get('current', {}).get('title')}\n"
        f"Description: {crawl_result.get('current', {}).get('description')}\n"
        "Findings:\n"
        + "\n".join(f"- {f[0]}: {f[1]} — {f[2]}" for f in findings)
        + "\n\nWrite concrete on-page SEO fix recommendations. Do not invent ranks or traffic."
    )
    text, model, role = generate_draft_body("on-page-seo", brief, writing_type="On-page fixes", tokens=600)
    jev = score_draft("on-page-seo", brief, text)
    draft = Draft(
        customer_id=user.id,
        kind="on-page-seo",
        brief=brief,
        body=text,
        status="approved" if jev.get("band") == "auto" else "waiting_for_writer",
        score=jev,
    )
    db.add(draft)
    db.add(Job(customer_id=user.id, kind="draft", status="done", detail={"kind": "on-page-seo", "model": model, "jev": jev}))
    db.commit()
    db.refresh(draft)

    # Queue as operator SiteChange so Approve → CMS execute is one hop away
    from app.models import SiteChange

    change = SiteChange(
        customer_id=user.id,
        draft_id=draft.id,
        source="on_page",
        opportunity=f"On-page SEO fixes for {crawl_result.get('current', {}).get('url') or body.link}",
        target_url=str(crawl_result.get("current", {}).get("url") or body.link),
        change_type="content",
        proposed={
            "title": (crawl_result.get("current") or {}).get("title") or "",
            "content": text,
            "draftBody": text,
            "metaDescription": (crawl_result.get("current") or {}).get("description") or "",
            "jev": jev,
            "findings": findings[:12],
            "targetUrl": str(crawl_result.get("current", {}).get("url") or body.link),
        },
        status="approved" if jev.get("band") == "auto" else "awaiting_approval",
    )
    db.add(change)
    db.commit()
    db.refresh(change)

    return {
        "crawl": crawl_result,
        "draft": {"id": draft.id, "body": text, "status": draft.status, "jev": jev, "model": model, "role": role},
        "change": {"id": change.id, "status": change.status},
    }
