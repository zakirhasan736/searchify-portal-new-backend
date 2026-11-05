"""Stripe Billing for customer subscriptions, plus HQ money helpers.

Affiliate and partner payouts are not sent through Stripe Connect. Admin approves
a statement, then pays by PayPal, Wise, or bank.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import config
from app.models import FeatureRecord, User

log = logging.getLogger("searchify.stripe")

PAID_STATUSES = {"trial", "active", "paid", "paused", "cancelled", "trial_ended"}
HOLD_CLEAR = ("hold", "cleared")


def configured() -> bool:
    return bool(getattr(config, "STRIPE_SECRET_KEY", "") or "")


def _stripe():
    if not configured():
        raise HTTPException(503, "Stripe is not configured. Set STRIPE_SECRET_KEY.")
    import stripe

    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def _feature(db: Session, user_id: int, kind: str) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    return dict((row.payload if row else {}) or {})


def _save(db: Session, user_id: int, kind: str, title: str, payload: dict) -> dict:
    row = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.customer_id == user_id, FeatureRecord.kind == kind)
        .order_by(FeatureRecord.id.desc())
        .first()
    )
    if row is None:
        row = FeatureRecord(customer_id=user_id, kind=kind, title=title, payload=payload)
        db.add(row)
    else:
        row.payload = payload
        row.title = title
    db.commit()
    return payload


def checkout_is_live(checkout: dict | None) -> bool:
    if not checkout:
        return False
    return str(checkout.get("status") or "").lower() in PAID_STATUSES


# Same rates the checkout page shows. Missing or unknown country adds no tax.
TAX_RATES = {"CA": 0.05, "US": 0.0, "GB": 0.20, "AU": 0.10}
# Stripe online card rate, passed through so the plan price is not reduced by the charge.
CARD_FEE_RATE = 0.029
CARD_FEE_FIXED_CENTS = 30


def normalize_billing(raw: str) -> str:
    return "annual" if str(raw or "").strip().lower() in {"annual", "year", "yearly"} else "monthly"


def _amount_cents(plan: dict, billing: str) -> int:
    monthly = int(plan.get("mo") or 0)
    if normalize_billing(billing) == "annual":
        # Yearly is 10 months: two months free.
        return monthly * 10 * 100
    return monthly * 100


def quote_cents(plan: dict, billing: str, country: str = "") -> dict:
    """Plan price plus tax plus the card processing charge, in cents.

    Tax is on the plan price. The processing line is 2.9% + $0.30 of the amount
    Stripe will charge, so a new plan, an upgrade, and a monthly or yearly renewal
    all collect the same two extras.
    """
    billing = normalize_billing(billing)
    base = _amount_cents(plan, billing)
    if base <= 0:
        base = 4900 * (10 if billing == "annual" else 1)
    rate = TAX_RATES.get(str(country or "").strip().upper(), 0.0)
    tax = int(round(base * rate))
    # ceil((base + tax + 30) / 0.971) using integer math.
    total = ((base + tax + CARD_FEE_FIXED_CENTS) * 1000 + 970) // 971
    fee = total - base - tax
    return {
        "baseCents": base,
        "taxCents": tax,
        "feeCents": fee,
        "totalCents": total,
        "taxRate": rate,
    }


def _meta(*, user: User, plan: dict, canonical: str, billing: str, founding: bool, trial_days: int, country: str, quote: dict) -> dict:
    return {
        "userId": str(user.id),
        "planId": str(plan.get("id") or ""),
        "canonical": canonical,
        "billing": normalize_billing(billing),
        "founding": "1" if founding else "0",
        "trialDays": str(int(trial_days or 0)),
        "country": str(country or "").strip().upper(),
        "taxCents": str(int(quote.get("taxCents") or 0)),
        "feeCents": str(int(quote.get("feeCents") or 0)),
        "baseCents": str(int(quote.get("baseCents") or 0)),
    }


def _charge_description(plan: dict, quote: dict) -> str:
    name = plan.get("name") or "plan"
    parts = [f"{name} ${quote['baseCents'] / 100:.2f}"]
    parts.append(f"card processing ${quote['feeCents'] / 100:.2f}")
    if quote.get("taxCents"):
        parts.append(f"tax ${quote['taxCents'] / 100:.2f}")
    return ", ".join(parts)


def _subscription_status(sub: dict, trial_days: int) -> str:
    raw = str(sub.get("status") or "")
    if raw == "trialing":
        return "trial"
    if raw == "past_due":
        return "past_due"
    if raw in {"canceled", "unpaid", "incomplete_expired"}:
        return "cancelled"
    if raw == "active":
        return "active"
    return "trial" if trial_days else "active"


def begin_checkout(
    db: Session,
    *,
    user: User,
    plan: dict,
    canonical: str,
    billing: str,
    email: str,
    trial_days: int,
    founding: bool,
    country: str = "",
) -> dict:
    """New card goes to Checkout. An existing subscription is retargeted in place."""
    billing = normalize_billing(billing)
    country = str(country or "").strip().upper()
    prior = _feature(db, user.id, "billing-checkout")
    same = (
        checkout_is_live(prior)
        and str(prior.get("planId") or "") == str(plan.get("id") or "")
        and normalize_billing(prior.get("billing")) == billing
        and prior.get("stripeSubscriptionId")
        and str(prior.get("country") or "").upper() == country
    )
    if same:
        return {
            "ok": True,
            "already": True,
            "provider": "stripe",
            "plan": plan,
            "canonical": canonical,
            "billing": billing,
            "detail": "You're already on this plan and billing period.",
        }
    if prior.get("stripeSubscriptionId") and checkout_is_live(prior):
        changed = _retarget_subscription(
            db,
            user=user,
            plan=plan,
            canonical=canonical,
            billing=billing,
            email=email,
            trial_days=trial_days,
            founding=founding,
            country=country,
            prior=prior,
        )
        if changed:
            return changed
    return start_customer_checkout(
        db,
        user=user,
        plan=plan,
        canonical=canonical,
        billing=billing,
        email=email,
        trial_days=trial_days,
        founding=founding,
        country=country,
        prior=prior,
    )


def _retarget_subscription(
    db: Session,
    *,
    user: User,
    plan: dict,
    canonical: str,
    billing: str,
    email: str,
    trial_days: int,
    founding: bool,
    country: str,
    prior: dict,
) -> dict | None:
    """Swap the price on the current subscription. Returns None if Stripe must collect a new card."""
    stripe = _stripe()
    sub_id = str(prior.get("stripeSubscriptionId") or "")
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        log.exception("stripe_subscription_lookup_failed", extra={"user_id": user.id})
        return None
    if str(sub.get("status") or "") != "trialing" or not trial_days:
        # A charge needs Checkout so the success URL can store the plan. In-place updates
        # are only for a free month that is still running (monthly <-> yearly on Solo).
        return None
    items = list(((sub.get("items") or {}).get("data")) or [])
    if not items:
        return None
    quote = quote_cents(plan, billing, country)
    amount = quote["totalCents"]
    interval = "year" if billing == "annual" else "month"
    meta = _meta(
        user=user, plan=plan, canonical=canonical, billing=billing,
        founding=founding, trial_days=trial_days, country=country, quote=quote,
    )
    try:
        product = stripe.Product.create(
            name=f"Searchify {plan.get('name') or canonical}",
            description=_charge_description(plan, quote)[:500],
        )
        price = stripe.Price.create(
            product=product.id,
            currency="usd",
            unit_amount=amount,
            recurring={"interval": interval},
        )
        item_ops = [{"id": items[0].get("id"), "price": price.id}]
        for extra in items[1:]:
            item_ops.append({"id": extra.get("id"), "deleted": True})
        kwargs = {
            "items": item_ops,
            "metadata": meta,
            "proration_behavior": "create_prorations",
            "payment_behavior": "pending_if_incomplete",
        }
        if str(sub.get("status") or "") == "trialing" and not trial_days:
            kwargs["trial_end"] = "now"
        updated = stripe.Subscription.modify(sub_id, **kwargs)
    except Exception:
        log.exception("stripe_subscription_update_failed", extra={"user_id": user.id})
        return None
    latest = updated.get("latest_invoice")
    if isinstance(latest, str) and latest:
        try:
            latest = stripe.Invoice.retrieve(latest)
        except Exception:
            latest = None
    amount_due = int((latest or {}).get("amount_due") or 0) if isinstance(latest, dict) or latest else 0
    invoice_status = str((latest or {}).get("status") or "") if latest else ""
    if str(updated.get("status") or "") in {"active", "trialing"} and amount_due == 0:
        payload = apply_subscription(
            db,
            user=user,
            plan_id=str(plan.get("id") or ""),
            canonical=canonical,
            billing=billing,
            trial_days=trial_days if str(updated.get("status") or "") == "trialing" else 0,
            founding=founding,
            email=email,
            stripe_customer=str(updated.get("customer") or prior.get("stripeCustomerId") or ""),
            stripe_subscription=sub_id,
            status=_subscription_status(updated, trial_days),
            country=country,
        )
        return {
            "ok": True,
            "updated": True,
            "provider": "stripe",
            "plan": plan,
            "canonical": canonical,
            "billing": billing,
            "trialDays": int(payload.get("trialDays") or 0),
            "founding": founding,
            "checkout": payload,
            "detail": "Subscription updated. Stripe will use the card already on file.",
            "next": "/app",
        }
    url = str((latest or {}).get("hosted_invoice_url") or "") if latest else ""
    if url and invoice_status in {"open", "draft"}:
        return {
            "ok": True,
            "provider": "stripe",
            "pending": True,
            "checkoutUrl": url,
            "plan": plan,
            "canonical": canonical,
            "billing": billing,
            "detail": "Confirm the new price on Stripe. The plan changes after that payment.",
            "next": "/app",
        }
    return None


def start_customer_checkout(
    db: Session,
    *,
    user: User,
    plan: dict,
    canonical: str,
    billing: str,
    email: str,
    trial_days: int,
    founding: bool,
    country: str = "",
    prior: dict | None = None,
) -> dict:
    """Create a Stripe Checkout subscription. Plan is applied on webhook / confirm."""
    stripe = _stripe()
    billing = normalize_billing(billing)
    country = str(country or "").strip().upper()
    prior = prior or {}
    interval = "year" if billing == "annual" else "month"
    quote = quote_cents(plan, billing, country)
    amount = quote["totalCents"]
    if amount <= 0 and not trial_days:
        raise HTTPException(400, "This plan has no Stripe price.")
    meta = _meta(
        user=user, plan=plan, canonical=canonical, billing=billing,
        founding=founding, trial_days=trial_days, country=country, quote=quote,
    )
    sub_data: dict = {"metadata": meta}
    if trial_days:
        sub_data["trial_period_days"] = int(trial_days)
    site = (config.PUBLIC_SITE_URL or "http://localhost:3000").rstrip("/")
    plan_key = plan.get("id") or canonical
    create_args = dict(
        mode="subscription",
        client_reference_id=str(user.id),
        success_url=f"{site}/app?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{site}/checkout?plan={plan_key}&billing={billing}",
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount,
                    "recurring": {"interval": interval},
                    "product_data": {
                        "name": f"Searchify {plan.get('name') or canonical}",
                        "description": _charge_description(plan, quote)[:500],
                    },
                },
            }
        ],
        subscription_data=sub_data,
        metadata=meta,
    )
    customer_id = str(prior.get("stripeCustomerId") or "").strip()
    mail = (email or user.email or "").strip()
    if customer_id:
        create_args["customer"] = customer_id
    elif mail:
        create_args["customer_email"] = mail
    try:
        session = stripe.checkout.Session.create(**create_args)
    except Exception:
        if not customer_id:
            raise
        create_args.pop("customer", None)
        if mail:
            create_args["customer_email"] = mail
        session = stripe.checkout.Session.create(**create_args)
    return {
        "ok": True,
        "live": True,
        "provider": "stripe",
        "checkoutUrl": session.url,
        "sessionId": session.id,
        "plan": plan,
        "canonical": canonical,
        "billing": billing,
        "trialDays": trial_days,
        "founding": founding,
        "amountCents": amount,
        "baseCents": quote["baseCents"],
        "taxCents": quote["taxCents"],
        "feeCents": quote["feeCents"],
        "detail": "Continue to Stripe to confirm the card. The plan is stored when Stripe confirms.",
        "next": "/app",
    }


def apply_subscription(
    db: Session,
    *,
    user: User,
    plan_id: str,
    canonical: str,
    billing: str,
    trial_days: int,
    founding: bool,
    email: str,
    stripe_customer: str = "",
    stripe_subscription: str = "",
    status: str | None = None,
    country: str = "",
) -> dict:
    """Persist a confirmed subscription onto the user, journey, and billing-checkout."""
    from app import site_profiles
    from app.routers.billing import BY_CANONICAL, BY_ID
    from app.routers.partners import referral_plan

    raw = (plan_id or canonical or "").strip().lower()
    plan = BY_ID.get(raw) or BY_CANONICAL.get(raw)
    if plan is None and canonical:
        plan = BY_CANONICAL.get(canonical) or BY_ID.get(canonical)
    if plan is None:
        raise HTTPException(400, "Unknown plan on the Stripe session.")
    canonical = plan["canonical"]
    user.plan = "scale" if canonical == "agency" else canonical
    user.site_limit = int(plan["siteLimit"] or 1)
    db.add(user)
    db.commit()

    journey = site_profiles.get_journey(db, user.id) or {}
    state = dict(journey.get("state") or {})
    state["planId"] = canonical
    state["billing"] = "annual" if billing == "annual" else "monthly"
    try:
        site_profiles.save_journey(db, user.id, state)
    except Exception:
        log.exception("stripe_journey_save_failed", extra={"user_id": user.id})

    prior = _feature(db, user.id, "billing-checkout")
    final_status = status or ("trial" if trial_days else "active")
    payload = {
        **prior,
        "planId": plan["id"],
        "canonical": canonical,
        "billing": state["billing"],
        "email": (email or user.email or "").strip().lower(),
        "status": final_status,
        "trialDays": int(trial_days or prior.get("trialDays") or 0),
        "founding": bool(founding),
        "provider": "stripe",
        "stripeCustomerId": stripe_customer or prior.get("stripeCustomerId") or "",
        "stripeSubscriptionId": stripe_subscription or prior.get("stripeSubscriptionId") or "",
        "country": (country or prior.get("country") or "").strip().upper(),
        "at": prior.get("at") or datetime.utcnow().isoformat() + "Z",
        "confirmedAt": datetime.utcnow().isoformat() + "Z",
    }
    _save(db, user.id, "billing-checkout", "Billing checkout", payload)
    referral_plan(db, user, canonical)
    return payload


def confirm_session(db: Session, user: User, session_id: str) -> dict:
    stripe = _stripe()
    session = stripe.checkout.Session.retrieve(session_id)
    owner = str((session.get("metadata") or {}).get("userId") or session.get("client_reference_id") or "")
    if owner and owner != str(user.id):
        raise HTTPException(403, "This checkout belongs to another account.")
    if session.get("status") not in {"complete"} and session.get("payment_status") not in {"paid", "no_payment_required"}:
        return {"ok": False, "detail": "Stripe has not confirmed this checkout yet.", "status": session.get("status")}
    meta = dict(session.get("metadata") or {})
    sub_id = session.get("subscription") or ""
    cust = session.get("customer") or ""
    trial_days = int(meta.get("trialDays") or 0)
    status = "trial" if trial_days else "active"
    if sub_id:
        sub = stripe.Subscription.retrieve(sub_id)
        if sub.get("status") == "trialing":
            status = "trial"
        elif sub.get("status") in {"active", "past_due"}:
            status = "active" if sub.get("status") == "active" else "past_due"
    prior = _feature(db, user.id, "billing-checkout")
    old_sub = str(prior.get("stripeSubscriptionId") or "")
    payload = apply_subscription(
        db,
        user=user,
        plan_id=meta.get("planId") or "",
        canonical=meta.get("canonical") or "",
        billing=meta.get("billing") or "monthly",
        trial_days=trial_days,
        founding=meta.get("founding") == "1",
        email=session.get("customer_email") or user.email or "",
        stripe_customer=str(cust or ""),
        stripe_subscription=str(sub_id or ""),
        status=status,
        country=str(meta.get("country") or ""),
    )
    if old_sub and sub_id and old_sub != str(sub_id):
        try:
            stripe.Subscription.delete(old_sub)
        except Exception:
            log.exception("stripe_old_sub_cancel_failed", extra={"user_id": user.id})
    return {"ok": True, "checkout": payload}


def _user_by_id(db: Session, raw) -> User | None:
    try:
        return db.get(User, int(raw))
    except (TypeError, ValueError):
        return None


def _already(db: Session, user_id: int, key: str) -> bool:
    seen = _feature(db, user_id, "stripe-events")
    return key in (seen.get("ids") or [])


def _mark(db: Session, user_id: int, key: str) -> None:
    seen = _feature(db, user_id, "stripe-events")
    ids = list(seen.get("ids") or [])
    if key not in ids:
        ids.append(key)
    _save(db, user_id, "stripe-events", "Stripe events", {"ids": ids[-200:]})


def handle_event(db: Session, event: dict) -> dict:
    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        user = _user_by_id(db, meta.get("userId") or obj.get("client_reference_id"))
        if user is None:
            return {"ok": False, "detail": "No user on session."}
        if _already(db, user.id, event.get("id") or obj.get("id") or ""):
            return {"ok": True, "duplicate": True}
        confirm_session(db, user, obj.get("id"))
        _mark(db, user.id, event.get("id") or obj.get("id"))
        return {"ok": True}
    if kind == "invoice.paid":
        return _on_invoice_paid(db, obj, event.get("id") or obj.get("id") or "")
    if kind == "invoice.payment_failed":
        return _set_status_from_invoice(db, obj, "past_due")
    if kind in {"customer.subscription.deleted", "customer.subscription.updated"}:
        return _on_subscription(db, obj, deleted=kind.endswith("deleted"))
    if kind == "charge.refunded":
        return _on_refund(db, obj, event.get("id") or obj.get("id") or "")
    return {"ok": True, "ignored": kind}


def _checkout_for_customer(db: Session, stripe_customer: str) -> tuple[User | None, dict]:
    if not stripe_customer:
        return None, {}
    rows = (
        db.query(FeatureRecord)
        .filter(FeatureRecord.kind == "billing-checkout")
        .order_by(FeatureRecord.id.desc())
        .all()
    )
    for row in rows:
        payload = dict(row.payload or {})
        if str(payload.get("stripeCustomerId") or "") == str(stripe_customer):
            user = db.get(User, row.customer_id)
            return user, payload
    return None, {}


def _on_invoice_paid(db: Session, invoice: dict, event_id: str) -> dict:
    from app.routers.partners import record_payment

    user, checkout = _checkout_for_customer(db, invoice.get("customer"))
    if user is None:
        meta = ((invoice.get("subscription_details") or {}).get("metadata")) or invoice.get("metadata") or {}
        user = _user_by_id(db, meta.get("userId"))
        checkout = _feature(db, user.id, "billing-checkout") if user else {}
    if user is None:
        return {"ok": False, "detail": "Invoice has no Searchify customer."}
    if event_id and _already(db, user.id, event_id):
        return {"ok": True, "duplicate": True}
    amount = round(float(invoice.get("amount_paid") or 0) / 100.0, 2)
    plan = checkout.get("canonical") or checkout.get("planId") or user.plan or ""
    if checkout:
        checkout = {**checkout, "status": "trial" if checkout.get("status") == "trial" and amount <= 0 else "active"}
        _save(db, user.id, "billing-checkout", "Billing checkout", checkout)
    commission = 0.0
    if amount > 0:
        from app import payout_policy

        payout_policy.record_revenue(db, amount=amount, plan=str(plan), user_id=user.id, kind="payment")
        commission = record_payment(db, user, amount, str(plan))
    if event_id:
        _mark(db, user.id, event_id)
    return {"ok": True, "amount": amount, "commission": commission}


def _set_status_from_invoice(db: Session, invoice: dict, status: str) -> dict:
    user, checkout = _checkout_for_customer(db, invoice.get("customer"))
    if user is None or not checkout:
        return {"ok": True, "ignored": True}
    checkout["status"] = status
    _save(db, user.id, "billing-checkout", "Billing checkout", checkout)
    return {"ok": True}


def _on_subscription(db: Session, sub: dict, *, deleted: bool) -> dict:
    user, checkout = _checkout_for_customer(db, sub.get("customer"))
    if user is None or not checkout:
        return {"ok": True, "ignored": True}
    incoming = str(sub.get("id") or "")
    current = str(checkout.get("stripeSubscriptionId") or "")
    if incoming and current and incoming != current:
        return {"ok": True, "ignored": "replaced"}
    if not deleted and sub.get("status") in {"active", "trialing"}:
        meta = dict(sub.get("metadata") or {})
        if meta.get("planId") or meta.get("canonical"):
            apply_subscription(
                db,
                user=user,
                plan_id=str(meta.get("planId") or ""),
                canonical=str(meta.get("canonical") or ""),
                billing=str(meta.get("billing") or checkout.get("billing") or "monthly"),
                trial_days=int(meta.get("trialDays") or 0) if sub.get("status") == "trialing" else 0,
                founding=meta.get("founding") == "1",
                email=checkout.get("email") or user.email or "",
                stripe_customer=str(sub.get("customer") or ""),
                stripe_subscription=str(sub.get("id") or ""),
                status="trial" if sub.get("status") == "trialing" else "active",
                country=str(meta.get("country") or checkout.get("country") or ""),
            )
            return {"ok": True, "applied": True}
    if deleted or sub.get("status") in {"canceled", "unpaid"}:
        checkout["status"] = "cancelled"
    elif sub.get("status") == "trialing":
        checkout["status"] = "trial"
    elif sub.get("status") == "active":
        checkout["status"] = "active"
    elif sub.get("status") == "past_due":
        checkout["status"] = "past_due"
    checkout["stripeSubscriptionId"] = sub.get("id") or checkout.get("stripeSubscriptionId")
    _save(db, user.id, "billing-checkout", "Billing checkout", checkout)
    return {"ok": True}


def _on_refund(db: Session, charge: dict, event_id: str) -> dict:
    from app.routers.partners import record_refund

    user, checkout = _checkout_for_customer(db, charge.get("customer"))
    if user is None:
        return {"ok": True, "ignored": True}
    if event_id and _already(db, user.id, event_id):
        return {"ok": True, "duplicate": True}
    amount = round(float(charge.get("amount_refunded") or 0) / 100.0, 2)
    plan = checkout.get("canonical") or user.plan or ""
    if amount > 0:
        from app import payout_policy

        payout_policy.record_revenue(db, amount=amount, plan=str(plan), user_id=user.id, kind="refund")
    commission = record_refund(db, user, amount, str(plan)) if amount > 0 else 0.0
    if event_id:
        _mark(db, user.id, event_id)
    return {"ok": True, "refunded": amount, "commission": commission}


def payout_minimum() -> float:
    try:
        return float(getattr(config, "AFFILIATE_PAYOUT_MINIMUM", 50) or 50)
    except (TypeError, ValueError):
        return 50.0


def money_snapshot(db: Session) -> dict:
    """System-owner view: Stripe balance, MRR, affiliate liability, dividends payable."""
    mrr = 0.0
    rows = db.query(FeatureRecord).filter(FeatureRecord.kind == "billing-checkout").all()
    seen: set[int] = set()
    for row in rows:
        if row.customer_id in seen:
            continue
        seen.add(row.customer_id)
        payload = dict(row.payload or {})
        if str(payload.get("status") or "") not in {"active", "trial", "paid"}:
            continue
        price = 0
        try:
            from app.routers.billing import BY_CANONICAL, BY_ID

            plan = BY_ID.get(payload.get("planId")) or BY_CANONICAL.get(payload.get("canonical"))
            price = float((plan or {}).get("mo") or 0)
            if payload.get("billing") == "annual":
                price = round(price * 10 / 12, 2)
        except Exception:
            price = 0
        if payload.get("status") != "trial":
            mrr += price
    hold = cleared = 0.0
    for row in db.query(FeatureRecord).filter(FeatureRecord.kind == "partner-ledger").all():
        totals = dict((row.payload or {}).get("totals") or {})
        hold += float(totals.get("hold") or 0)
        cleared += float(totals.get("cleared") or 0)
    dividends = _feature_global_admin(db)
    payable = 0.0
    for run in dividends.get("runs") or []:
        if run.get("status") in {"declared", "approved"}:
            payable += float(run.get("net") or 0)
    balance = None
    if configured():
        try:
            stripe = _stripe()
            bal = stripe.Balance.retrieve()
            available = sum(int(b.get("amount") or 0) for b in (bal.get("available") or []) if b.get("currency") == "usd")
            pending = sum(int(b.get("amount") or 0) for b in (bal.get("pending") or []) if b.get("currency") == "usd")
            balance = {"available": round(available / 100, 2), "pending": round(pending / 100, 2), "currency": "usd"}
        except Exception as exc:
            balance = {"error": str(exc)}
    return {
        "ok": True,
        "stripeConfigured": configured(),
        "balance": balance,
        "mrr": round(mrr, 2),
        "affiliateHold": round(hold, 2),
        "affiliateCleared": round(cleared, 2),
        "affiliateLiability": round(hold + cleared, 2),
        "dividendsPayable": round(payable, 2),
        "payoutMinimum": payout_minimum(),
        "dividends": dividends.get("runs") or [],
    }


def _admin(db: Session) -> User | None:
    return db.query(User).filter(User.role == "ROLE_ADMIN").order_by(User.id.asc()).first()


def _feature_global_admin(db: Session) -> dict:
    admin = _admin(db)
    if admin is None:
        return {}
    return _feature(db, admin.id, "hq-dividends")


def _save_dividends(db: Session, payload: dict) -> dict:
    admin = _admin(db)
    if admin is None:
        raise HTTPException(500, "No admin account to store dividends.")
    return _save(db, admin.id, "hq-dividends", "HQ dividends", payload)


def _parse_shares(profile: dict) -> float:
    raw = str(profile.get("shares") or "").replace(",", "").strip()
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def declare_dividend(db: Session, *, pool: float, note: str = "") -> dict:
    """Split a cash pool across investors by share count (or ownership %). Admin must approve before pay."""
    from app.routers.investors import list_portal_investors

    if pool <= 0:
        raise HTTPException(400, "Enter a dividend pool greater than zero.")
    holders = list_portal_investors(db)
    weights = []
    for holder in holders:
        user = db.get(User, holder["userId"])
        if user is None:
            continue
        from app.routers.investors import _profile_for

        profile = _profile_for(db, user)
        shares = _parse_shares(profile)
        pct = float(profile.get("pct") or 0)
        weights.append((user, profile, shares, pct))
    share_sum = sum(item[2] for item in weights)
    pct_sum = sum(item[3] for item in weights)
    lines = []
    net_total = 0.0
    for user, profile, shares, pct in weights:
        if share_sum > 0:
            fraction = shares / share_sum
        elif pct_sum > 0:
            fraction = pct / pct_sum
        else:
            fraction = 1 / max(1, len(weights))
        gross = round(pool * fraction, 2)
        withhold_rate = float(profile.get("wh") or 0)
        withhold = round(gross * withhold_rate, 2)
        net = round(gross - withhold, 2)
        net_total += net
        lines.append(
            {
                "userId": user.id,
                "email": user.email,
                "name": profile.get("displayName") or user.username,
                "gross": gross,
                "withhold": withhold,
                "net": net,
                "status": "declared",
            }
        )
    store = _feature_global_admin(db)
    runs = list(store.get("runs") or [])
    run = {
        "id": f"div-{int(datetime.utcnow().timestamp())}",
        "status": "declared",
        "pool": round(pool, 2),
        "net": round(net_total, 2),
        "note": note,
        "holders": lines,
        "declaredAt": datetime.utcnow().isoformat() + "Z",
    }
    runs.insert(0, run)
    _save_dividends(db, {"runs": runs[:40]})
    return {"ok": True, "run": run}


def approve_dividend(db: Session, run_id: str) -> dict:
    store = _feature_global_admin(db)
    runs = list(store.get("runs") or [])
    for run in runs:
        if run.get("id") == run_id:
            if run.get("status") != "declared":
                raise HTTPException(400, "Only a declared dividend can be approved.")
            run["status"] = "approved"
            run["approvedAt"] = datetime.utcnow().isoformat() + "Z"
            for line in run.get("holders") or []:
                line["status"] = "approved"
            _save_dividends(db, {"runs": runs})
            return {"ok": True, "run": run}
    raise HTTPException(404, "Dividend run not found.")


def mark_dividend_paid(db: Session, run_id: str, reference: str) -> dict:
    """Money is sent by bank or Wise. This only records the admin confirmation — no Stripe Connect."""
    if not (reference or "").strip():
        raise HTTPException(400, "Enter the bank or Wise reference.")
    store = _feature_global_admin(db)
    runs = list(store.get("runs") or [])
    for run in runs:
        if run.get("id") == run_id:
            if run.get("status") != "approved":
                raise HTTPException(400, "Approve the dividend before marking it paid.")
            run["status"] = "paid"
            run["paidAt"] = datetime.utcnow().isoformat() + "Z"
            run["reference"] = reference.strip()
            for line in run.get("holders") or []:
                line["status"] = "paid"
                line["reference"] = reference.strip()
            _save_dividends(db, {"runs": runs})
            return {"ok": True, "run": run}
    raise HTTPException(404, "Dividend run not found.")


def dividend_for_user(db: Session, user_id: int) -> dict | None:
    store = _feature_global_admin(db)
    for run in store.get("runs") or []:
        for line in run.get("holders") or []:
            if int(line.get("userId") or 0) == int(user_id):
                return {"run": run, "line": line}
    return None
