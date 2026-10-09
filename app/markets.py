"""Shared target-market configuration for every SEO tool.

One saved market per website. Tools ask get_market_context() instead of inventing
their own country, language, or provider location fields.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app import locations, site_profiles
from app.models import CmsConnection, FeatureRecord, GoogleConnection, User

# Dashboard region picker. Extensible — not every country DataForSEO knows.
TARGET_COUNTRIES: list[dict] = [
    {"iso": "CA", "name": "Canada", "language": "en", "flag": "🇨🇦"},
    {"iso": "US", "name": "United States", "language": "en", "flag": "🇺🇸"},
    {"iso": "GB", "name": "United Kingdom", "language": "en", "flag": "🇬🇧"},
    {"iso": "AU", "name": "Australia", "language": "en", "flag": "🇦🇺"},
    {"iso": "NZ", "name": "New Zealand", "language": "en", "flag": "🇳🇿"},
    {"iso": "IN", "name": "India", "language": "en", "flag": "🇮🇳"},
    {"iso": "AE", "name": "United Arab Emirates", "language": "en", "flag": "🇦🇪"},
]
BY_ISO = {row["iso"]: row for row in TARGET_COUNTRIES}

# Country-code and multi-label TLDs only. Generic TLDs never imply a country.
TLD_SUGGESTIONS: dict[str, str] = {
    "ca": "CA",
    "uk": "GB",
    "co.uk": "GB",
    "org.uk": "GB",
    "ac.uk": "GB",
    "au": "AU",
    "com.au": "AU",
    "net.au": "AU",
    "nz": "NZ",
    "co.nz": "NZ",
    "in": "IN",
    "co.in": "IN",
    "ae": "AE",
    "us": "US",
}
GENERIC_TLDS = {"com", "org", "net", "io", "co", "app", "dev", "ai", "xyz", "info", "biz", "online", "site", "tech"}

KIND = "site-market"
SOURCES = ("user", "onboarding", "profile", "domain", "unset")

# Which tools treat results as market-dependent for cache/refresh labelling.
MARKET_DEPENDENT = {
    "keywords", "visibility", "competitors", "serp", "rank", "meta", "local",
}
MARKET_INDEPENDENT = {"backlinks", "audit_crawl", "crawl"}


class MarketError(ValueError):
    """Invalid market input. Safe to show to the owner."""


def countries() -> list[dict]:
    return list(TARGET_COUNTRIES)


def country_by_iso(iso: str) -> dict | None:
    return BY_ISO.get((iso or "").strip().upper())


def hostname(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw and not raw.startswith("//"):
        raw = f"https://{raw}"
    try:
        host = urlparse(raw).hostname or ""
    except ValueError:
        host = ""
    return host.lower().removeprefix("www.")


def registrable_labels(host: str) -> list[str]:
    """Hostname labels after stripping obvious multi-part public suffixes we care about."""
    host = hostname(host) if "://" in (host or "") or "/" in (host or "") else (host or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return [host] if host else []
    return [part for part in host.split(".") if part]


def suggest_iso_from_host(host: str) -> str | None:
    """TLD → ISO for unambiguous country domains. Never maps .com/.org/.net/.io to US."""
    labels = registrable_labels(host)
    if len(labels) < 2:
        return None
    # Prefer two-label suffixes (.co.uk) then one-label (.ca).
    two = f"{labels[-2]}.{labels[-1]}"
    if two in TLD_SUGGESTIONS:
        return TLD_SUGGESTIONS[two]
    one = labels[-1]
    if one in GENERIC_TLDS:
        return None
    return TLD_SUGGESTIONS.get(one)


def _title(host: str = "", site_id=None) -> str:
    host = site_profiles.host_of(host) if host else ""
    if host:
        return f"Site market {host}"[:200]
    if site_id is not None:
        return f"Site market id:{site_id}"[:200]
    return "Site market"


def _record(db: Session, user_id: int, host: str = "", site_id=None) -> FeatureRecord | None:
    titles = []
    if host:
        titles.append(_title(host=host))
    if site_id is not None:
        titles.append(_title(site_id=site_id))
    if not titles:
        return None
    return (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == KIND, FeatureRecord.title.in_(titles))
        .order_by(FeatureRecord.id.desc())
        .first()
    )


def _empty(host: str = "", site_id=None) -> dict:
    return {
        "host": site_profiles.host_of(host),
        "siteId": site_id,
        "countryIso": "",
        "countryName": "",
        "language": "en",
        "languages": ["en"],
        "city": "",
        "region": "",
        "serviceArea": "",
        "source": "unset",
        "explicit": False,
        "domainSuggestion": suggest_iso_from_host(host) if host else None,
        "needsChoice": True,
        "updatedAt": None,
        "version": 0,
    }


def _from_location(place: locations.Location, *, source: str, host: str = "", site_id=None, explicit: bool = False) -> dict:
    row = country_by_iso(place.iso) if place.iso else None
    language = place.language or (row["language"] if row else "en")
    return {
        "host": site_profiles.host_of(host),
        "siteId": site_id,
        "countryIso": place.iso or "",
        "countryName": place.country or (row["name"] if row else ""),
        "language": language,
        "languages": [language],
        "city": place.city or "",
        "region": place.region or "",
        "serviceArea": "",
        "source": source,
        "explicit": explicit,
        "domainSuggestion": suggest_iso_from_host(host) if host else None,
        "needsChoice": not bool(place.iso) and not place.worldwide,
        "updatedAt": None,
        "version": 0,
        "worldwide": bool(place.worldwide),
    }


def _load_saved(db: Session, user_id: int, host: str = "", site_id=None) -> dict | None:
    row = _record(db, user_id, host=host, site_id=site_id)
    if not row or not row.payload:
        return None
    data = dict(row.payload)
    if not data.get("countryIso"):
        return None
    data.setdefault("domainSuggestion", suggest_iso_from_host(host) if host else None)
    data["needsChoice"] = False
    return data


def _onboarding_place(answers: dict) -> locations.Location | None:
    market = (answers or {}).get("market") or ""
    reach = (answers or {}).get("reach") or ""
    if not market and not reach:
        return None
    try:
        return locations.resolve(market, reach=reach, allow_worldwide=True)
    except locations.LocationError:
        return None


def _normalize_host(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw.startswith("sc-domain:"):
        raw = raw.split(":", 1)[1]
    return site_profiles.host_of(raw)


def user_owns_host(db: Session, user: User, host: str) -> bool:
    """True when the host is in the journey, a connected CMS site, or the selected GSC property."""
    host = _normalize_host(host)
    if not host:
        return False
    journey = site_profiles.get_journey(db, user.id) or {}
    state = (journey.get("state") if isinstance(journey.get("state"), dict) else journey) or {}
    for site in state.get("sites") or []:
        if _normalize_host(((site.get("answers") or {}).get("site") or "")) == host:
            return True
    cms = (
        db.query(CmsConnection)
        .filter(CmsConnection.customer_id == user.id, CmsConnection.status == "connected")
        .all()
    )
    for row in cms:
        creds = row.credentials if isinstance(row.credentials, dict) else {}
        candidates = (
            row.site_url,
            row.label,
            creds.get("siteUrl"),
            creds.get("site_url"),
            (row.meta or {}).get("siteUrl") if isinstance(row.meta, dict) else "",
        )
        if any(_normalize_host(str(value or "")) == host for value in candidates):
            return True
    google = (
        db.query(GoogleConnection)
        .filter(GoogleConnection.customer_id == user.id)
        .order_by(GoogleConnection.id.desc())
        .all()
    )
    for row in google:
        if _normalize_host(row.gsc_site_url or "") == host:
            return True
    return False


def resolve_for_site(db: Session, user: User, *, site: str = "", site_id=None) -> dict:
    """Apply precedence: user save → onboarding → profile → domain → needs choice."""
    host = site_profiles.host_of(site)
    journey = site_profiles.get_journey(db, user.id) or {}
    state = (journey.get("state") if isinstance(journey.get("state"), dict) else journey) or {}
    sites = state.get("sites") or []
    site_row = None
    if site_id is not None:
        site_row = next((s for s in sites if str(s.get("id")) == str(site_id)), None)
    if site_row is None and host:
        site_row = next((s for s in sites if site_profiles.host_of((s.get("answers") or {}).get("site") or "") == host), None)
    if site_row is None and state.get("activeId") is not None and not host:
        site_row = next((s for s in sites if str(s.get("id")) == str(state.get("activeId"))), None)
    answers = (site_row or {}).get("answers") or {}
    if not host:
        host = site_profiles.host_of(answers.get("site") or "")
    sid = (site_row or {}).get("id") if site_row else site_id
    owned = site_row is not None or user_owns_host(db, user, host)

    # Only resolve a market for websites that belong to this workspace.
    if not owned:
        out = _empty(host, sid)
        out["domainSuggestion"] = suggest_iso_from_host(host) if host else None
        return out

    saved = _load_saved(db, user.id, host=host, site_id=sid)
    if saved and saved.get("explicit"):
        return {**saved, "siteId": sid or saved.get("siteId"), "host": host}

    # Explicit ISO on the journey answers counts as a user/onboarding selection.
    answer_iso = (answers.get("targetCountry") or "").strip().upper()
    if answer_iso and country_by_iso(answer_iso):
        row = country_by_iso(answer_iso)
        place = locations.resolve(row["name"])
        out = _from_location(place, source="user" if answers.get("marketSource") == "user" else "onboarding",
                             host=host, site_id=sid, explicit=answers.get("marketSource") == "user")
        if saved and not saved.get("explicit"):
            out["city"] = saved.get("city") or out["city"]
        return out

    if saved:
        return {**saved, "siteId": sid or saved.get("siteId"), "host": host}

    place = _onboarding_place(answers)
    if place and place.iso:
        return _from_location(place, source="onboarding", host=host, site_id=sid, explicit=False)

    profile = site_profiles.profile_for_host(db, user.id, host) if host else None
    if profile:
        loc = profile.get("location") or {}
        if loc.get("ok") and loc.get("iso"):
            try:
                place = locations.Location(
                    country=loc.get("country") or "",
                    iso=loc["iso"],
                    language=loc.get("language") or "en",
                    city=loc.get("city") or "",
                    region=loc.get("region") or "",
                    source="profile",
                )
                return _from_location(place, source="profile", host=host, site_id=sid, explicit=False)
            except Exception:
                pass

    suggested = suggest_iso_from_host(host) if host else None
    if suggested and country_by_iso(suggested):
        place = locations.resolve(country_by_iso(suggested)["name"])
        return _from_location(place, source="domain", host=host, site_id=sid, explicit=False)

    out = _empty(host, sid)
    out["domainSuggestion"] = suggested
    out["needsChoice"] = True
    return out


def save_market(
    db: Session,
    user: User,
    *,
    site: str = "",
    site_id=None,
    country_iso: str,
    language: str = "",
    city: str = "",
    region: str = "",
    service_area: str = "",
) -> dict:
    """Persist an explicit user market selection for this website."""
    iso = (country_iso or "").strip().upper()
    row = country_by_iso(iso)
    if not row:
        raise MarketError(f"Unsupported target country “{country_iso}”. Choose one of: {', '.join(BY_ISO)}.")
    host = site_profiles.host_of(site)
    journey = site_profiles.get_journey(db, user.id) or {}
    state = (journey.get("state") if isinstance(journey.get("state"), dict) else journey) or {}
    sites = list(state.get("sites") or [])
    site_row = None
    if site_id is not None:
        site_row = next((s for s in sites if str(s.get("id")) == str(site_id)), None)
    if site_row is None and host:
        site_row = next((s for s in sites if site_profiles.host_of((s.get("answers") or {}).get("site") or "") == host), None)
    if site_row is not None:
        host = host or site_profiles.host_of((site_row.get("answers") or {}).get("site") or "")
    if not host:
        raise MarketError("Add the website address before choosing a target market.")
    if site_row is None and not user_owns_host(db, user, host):
        raise MarketError("That website is not in this workspace.")

    place = locations.resolve(row["name"])
    lang = (language or row["language"] or place.language or "en").strip().lower()[:8]
    now = datetime.utcnow().isoformat(timespec="seconds")
    sid = site_row.get("id") if site_row else site_id
    previous = _load_saved(db, user.id, host=host, site_id=sid) or {}
    version = int(previous.get("version") or 0) + 1
    payload = {
        "host": host,
        "siteId": sid,
        "countryIso": iso,
        "countryName": row["name"],
        "language": lang,
        "languages": list(dict.fromkeys([lang, *(previous.get("languages") or [])]))[:5],
        "city": (city or "").strip()[:80],
        "region": (region or "").strip()[:80],
        "serviceArea": (service_area or "").strip()[:200],
        "source": "user",
        "explicit": True,
        "domainSuggestion": suggest_iso_from_host(host),
        "needsChoice": False,
        "updatedAt": now,
        "version": version,
    }
    title = _title(host=host, site_id=sid)
    existing = _record(db, user.id, host=host, site_id=sid)
    if existing is None:
        db.add(FeatureRecord(customer_id=user.id, kind=KIND, title=title, payload=payload, status="stored"))
    else:
        existing.payload = payload
        existing.status = "stored"
        existing.title = title

    # Keep journey answers aligned when this host is also an onboarding site.
    if site_row is not None:
        answers = dict(site_row.get("answers") or {})
        answers["targetCountry"] = iso
        answers["marketSource"] = "user"
        old_market = answers.get("market") or ""
        try:
            old_place = locations.resolve(old_market) if old_market else None
        except locations.LocationError:
            old_place = None
        if old_place and old_place.iso == iso and (old_place.city or old_place.region):
            answers["market"] = old_place.label
            if not payload["city"]:
                payload["city"] = old_place.city
            if not payload["region"]:
                payload["region"] = old_place.region
        else:
            answers["market"] = row["name"]
        site_row["answers"] = site_profiles.clean_answers({**answers, "market": answers["market"], "targetCountry": iso, "marketSource": "user"})
        site_row["answers"] = {**site_row["answers"], "targetCountry": iso, "marketSource": "user"}
        for i, s in enumerate(sites):
            if str(s.get("id")) == str(site_row.get("id")):
                sites[i] = site_row
                break
        state = {**state, "sites": sites}
        site_profiles.save_journey(db, user.id, state)
        existing = _record(db, user.id, host=host, site_id=sid)
        if existing is None:
            db.add(FeatureRecord(customer_id=user.id, kind=KIND, title=title, payload=payload, status="stored"))
        else:
            existing.payload = payload
            existing.title = title
    db.commit()
    return payload


def get_market_context(
    db: Session,
    user: User,
    *,
    site: str = "",
    site_id=None,
    tool_name: str = "",
    location_override: str = "",
) -> dict:
    """Authoritative market + provider adapters for one tool."""
    if location_override and location_override.strip():
        place = locations.resolve(location_override.strip())
        base = _from_location(place, source="override", host=site, site_id=site_id, explicit=True)
    else:
        base = resolve_for_site(db, user, site=site, site_id=site_id)
        if not base.get("countryIso") and not base.get("worldwide"):
            raise locations.LocationError(
                "Choose a target country in the dashboard header so research uses the right market."
            )
        if base.get("countryIso"):
            place = locations.resolve(
                ", ".join(p for p in (base.get("city"), base.get("region"), base.get("countryName")) if p)
                or base["countryName"]
            )
        else:
            place = locations.Location(country="", iso="", language=base.get("language") or "en", worldwide=True, source="market:worldwide")

    tool = (tool_name or "").strip().lower()
    if tool in MARKET_DEPENDENT and place.worldwide and not place.iso:
        raise locations.LocationError(
            "Your market is worldwide. Choose one country in the dashboard header so search data uses a real location."
        )
    adapters = {
        "dataforseo_labs": {
            "location_name": place.country if place.iso else "",
            "language_code": base.get("language") or place.language or "en",
        },
        "dataforseo_serp": {
            "location_name": place.country if place.iso else "",
            "language_code": base.get("language") or place.language or "en",
        },
        "dataforseo_ai": {
            "iso": place.iso or "",
            "city": base.get("city") or place.city or "",
            # Engines that reject country must omit these — llm_requests.build enforces that.
        },
        "backlinks": {"scope": "domain", "countryFilter": False},
        "audit_crawl": {"marketDependent": False},
        "gsc": {"useCountryDimension": False, "note": "GSC uses the connected property, not the SEO target market."},
        "ga4": {"useCountryDimension": False, "note": "GA4 uses the connected property, not the SEO target market."},
    }
    return {
        **base,
        "place": place.to_dict(),
        "label": place.label if place.iso or place.worldwide else base.get("countryName") or "",
        "tool": tool,
        "marketDependent": tool in MARKET_DEPENDENT,
        "adapters": adapters,
        "provider": adapters.get(
            {
                "keywords": "dataforseo_labs",
                "visibility": "dataforseo_ai",
                "competitors": "dataforseo_serp",
                "serp": "dataforseo_serp",
                "rank": "dataforseo_serp",
                "backlinks": "backlinks",
                "audit": "audit_crawl",
                "gsc": "gsc",
                "ga4": "ga4",
            }.get(tool, "dataforseo_labs")
        ),
    }
