"""Google Ads API (read-only) — list accounts and pull campaign metrics."""

from __future__ import annotations

import re

import httpx

from app import config

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


def ads_configured() -> bool:
    return bool(config.GOOGLE_ADS_DEVELOPER_TOKEN)


def _version() -> str:
    return (config.GOOGLE_ADS_API_VERSION or "v19").strip() or "v19"


def _headers(access_token: str, login_customer_id: str | None = None) -> dict:
    if not config.GOOGLE_ADS_DEVELOPER_TOKEN:
        raise RuntimeError(
            "Add GOOGLE_ADS_DEVELOPER_TOKEN to the backend .env. Enable the API in Cloud, then create the token in Google Ads → Tools → API Center."
        )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": config.GOOGLE_ADS_DEVELOPER_TOKEN,
        "Content-Type": "application/json",
    }
    login = (login_customer_id or config.GOOGLE_ADS_LOGIN_CUSTOMER_ID or "").replace("-", "").strip()
    if login:
        headers["login-customer-id"] = login
    return headers


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))


def list_accessible_customers(access_token: str, login_customer_id: str | None = None) -> list[str]:
    url = f"https://googleads.googleapis.com/{_version()}/customers:listAccessibleCustomers"
    with httpx.Client(timeout=40.0) as client:
        response = client.get(url, headers=_headers(access_token, login_customer_id))
    if response.status_code >= 400:
        raise RuntimeError(f"Ads accounts failed: {response.text[:400]}")
    names = response.json().get("resourceNames") or []
    ids = []
    for name in names:
        cid = _digits(str(name).split("/")[-1])
        if cid:
            ids.append(cid)
    return ids


def search_ads(access_token: str, customer_id: str, query: str, login_customer_id: str | None = None) -> list[dict]:
    cid = _digits(customer_id)
    if not cid:
        return []
    url = f"https://googleads.googleapis.com/{_version()}/customers/{cid}/googleAds:search"
    with httpx.Client(timeout=45.0) as client:
        response = client.post(
            url,
            headers=_headers(access_token, login_customer_id or cid),
            json={"query": query},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Ads query failed: {response.text[:400]}")
    return response.json().get("results") or []


def describe_customer(access_token: str, customer_id: str, login_customer_id: str | None = None) -> dict:
    cid = _digits(customer_id)
    rows = search_ads(
        access_token,
        cid,
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.manager, customer.test_account FROM customer LIMIT 1",
        login_customer_id,
    )
    customer = (rows[0] or {}).get("customer") if rows else {}
    return {
        "customerId": str(customer.get("id") or cid),
        "descriptiveName": customer.get("descriptiveName") or f"Account {cid}",
        "currencyCode": customer.get("currencyCode") or "",
        "manager": bool(customer.get("manager")),
        "testAccount": bool(customer.get("testAccount")),
    }


def list_ads_accounts(access_token: str, login_customer_id: str | None = None) -> list[dict]:
    accounts = []
    for cid in list_accessible_customers(access_token, login_customer_id):
        try:
            accounts.append(describe_customer(access_token, cid, login_customer_id))
        except Exception:
            accounts.append(
                {
                    "customerId": cid,
                    "descriptiveName": f"Account {cid}",
                    "currencyCode": "",
                    "manager": False,
                    "testAccount": False,
                }
            )
    return accounts


def _money(micros) -> float:
    try:
        return round(int(micros or 0) / 1_000_000, 2)
    except (TypeError, ValueError):
        return 0.0


def fetch_campaigns(access_token: str, customer_id: str, login_customer_id: str | None = None) -> list[list]:
    rows = search_ads(
        access_token,
        customer_id,
        (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions "
            "FROM campaign WHERE segments.date DURING LAST_30_DAYS "
            "ORDER BY metrics.cost_micros DESC LIMIT 25"
        ),
        login_customer_id,
    )
    out = []
    for row in rows:
        campaign = row.get("campaign") or {}
        metrics = row.get("metrics") or {}
        out.append(
            [
                campaign.get("name") or f"Campaign {campaign.get('id')}",
                campaign.get("status") or "—",
                _money(metrics.get("costMicros")),
                metrics.get("clicks") or 0,
                metrics.get("impressions") or 0,
                metrics.get("conversions") or 0,
            ]
        )
    return out


def fetch_search_terms(access_token: str, customer_id: str, login_customer_id: str | None = None) -> list[list]:
    rows = search_ads(
        access_token,
        customer_id,
        (
            "SELECT search_term_view.search_term, metrics.clicks, metrics.impressions, metrics.cost_micros "
            "FROM search_term_view WHERE segments.date DURING LAST_30_DAYS "
            "ORDER BY metrics.clicks DESC LIMIT 20"
        ),
        login_customer_id,
    )
    out = []
    for row in rows:
        term = (row.get("searchTermView") or {}).get("searchTerm") or "—"
        metrics = row.get("metrics") or {}
        out.append([term, metrics.get("clicks") or 0, metrics.get("impressions") or 0, _money(metrics.get("costMicros"))])
    return out
