"""Stripe adapter.

Stripe API specifics that drove the design:

- Auth = `Authorization: Bearer sk_...` (secret key). Restricted keys with
  read-only scopes work too — preferred for production.
- Cursor pagination via `starting_after=<last_id>` (not page numbers). Response
  shape is `{object: "list", data: [...], has_more: bool, url}`.
- /v1/charges is the simplest "order" surface — every successful payment is
  a charge, regardless of whether it came from a one-shot Checkout or a
  subscription invoice. We treat each charge as an order.
- Amounts are integer cents (Stripe's `amount` field). Currency is lowercase.
- A charge is refunded when `refunded=true`; partially when `amount_refunded > 0
  and refunded=false`. We map both to "refunded" for revenue accounting
  conservatism (a partial refund still erodes the recognized amount).
- Subscription status enum from Stripe: active, canceled, incomplete,
  incomplete_expired, past_due, paused, trialing, unpaid.
- For MRR: subscription `items.data[0].price.unit_amount` × `interval_count` →
  normalized to monthly. We read the embedded plan/price from the subscription
  object (Stripe expands plan-like data by default on /v1/subscriptions).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from ..models import (
    Customer,
    Money,
    Order,
    OrderStatus,
    Subscription,
    SubscriptionStatus,
)
from .base import RevenueAdapter

STRIPE_API_BASE = "https://api.stripe.com/v1"

_STRIPE_SUB_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "paused": "paused",
    "canceled": "cancelled",
    "incomplete": "unpaid",
    "incomplete_expired": "expired",
    "unpaid": "unpaid",
}

# Stripe price.recurring.interval → months
_RECURRING_TO_MONTHS: dict[str, int] = {
    "day": 1,  # Daily plans are rare; treat as monthly for MRR plumbing.
    "week": 1,
    "month": 1,
    "year": 12,
}


def _ts_to_dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        return None


def _order_status_from_charge(charge: dict[str, Any]) -> OrderStatus:
    if charge.get("refunded"):
        return "refunded"
    # Partial refund — conservative: treat as refunded for revenue.
    if int(charge.get("amount_refunded") or 0) > 0:
        return "partial_refund"
    status = (charge.get("status") or "").lower()
    if status == "succeeded":
        return "paid"
    if status == "pending":
        return "pending"
    return "failed"


def _customer_email(charge: dict[str, Any]) -> str:
    # Email lives on either `billing_details.email` or the expanded `customer.email`.
    email = (charge.get("billing_details") or {}).get("email")
    if email:
        return email.lower()
    customer = charge.get("customer")
    if isinstance(customer, dict):
        return (customer.get("email") or "").lower()
    receipt_email = charge.get("receipt_email")
    return (receipt_email or "").lower()


class StripeAdapter(RevenueAdapter):
    """Production Stripe adapter."""

    provider = "stripe"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_pages: int = 50,
        page_size: int = 100,
    ) -> None:
        if not api_key:
            raise ValueError("StripeAdapter requires a non-empty API key")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=STRIPE_API_BASE,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Stripe-Version": "2025-04-30.basil",  # pin a stable API version
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._max_pages = max_pages
        self._page_size = page_size

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "StripeAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get_with_retry(
        self, path: str, params: dict[str, Any] | None = None, attempts: int = 3
    ) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600 and i < attempts - 1:
                    last = e
                    await asyncio.sleep(2**i)
                    continue
                raise
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if i < attempts - 1:
                    await asyncio.sleep(2**i)
                    continue
                raise
        if last:
            raise last
        raise RuntimeError(f"Stripe GET {path} failed without exception")

    async def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk Stripe's cursor pagination (`starting_after`)."""
        base_params: dict[str, Any] = dict(params or {})
        base_params.setdefault("limit", self._page_size)
        last_id: str | None = None
        pages = 0
        while pages < self._max_pages:
            params_for_call = dict(base_params)
            if last_id:
                params_for_call["starting_after"] = last_id
            payload = await self._get_with_retry(path, params=params_for_call)
            items = payload.get("data") or []
            if not items:
                return
            for item in items:
                yield item
            if not payload.get("has_more"):
                return
            last_id = items[-1].get("id")
            if not last_id:
                return
            pages += 1

    # ------------------------------------------------------------------ orders

    async def list_orders(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[Order]:
        # Stripe /charges supports `created[gte]` and `created[lte]` as Unix ts.
        # We pass those for upstream narrowing, then re-filter precisely below.
        params: dict[str, Any] = {"expand[]": "data.customer"}
        if since:
            params["created[gte]"] = int(since.timestamp())
        if until:
            params["created[lte]"] = int(until.timestamp())
        async for charge in self._paginate("/charges", params=params):
            order = self._order_from_charge(charge)
            created = order.created_at
            if since and created < since:
                continue
            if until and created > until:
                continue
            yield order

    def _order_from_charge(self, charge: dict[str, Any]) -> Order:
        currency = (charge.get("currency") or "usd").upper()
        amount_cents = int(charge.get("amount") or 0)
        refunded_cents = int(charge.get("amount_refunded") or 0)
        # Stripe doesn't expose per-charge fee on /v1/charges unless balance_transaction
        # is expanded; v0.2 leaves fee None and lets the consumer infer it from payouts.
        net_cents = max(amount_cents - refunded_cents, 0)
        created = _ts_to_dt(charge.get("created")) or datetime.now(timezone.utc)
        description = charge.get("description") or (
            (charge.get("metadata") or {}).get("product_name")
        )
        refunded_at = None
        if charge.get("refunded") and (charge.get("refunds") or {}).get("data"):
            first_refund = charge["refunds"]["data"][0]
            refunded_at = _ts_to_dt(first_refund.get("created"))
        return Order(
            provider="stripe",
            provider_order_id=str(charge.get("id") or ""),
            customer_email=_customer_email(charge),
            status=_order_status_from_charge(charge),
            gross=Money(amount_cents=amount_cents, currency=currency),
            fee=None,
            net=Money(amount_cents=net_cents, currency=currency),
            product_name=description,
            created_at=created,
            refunded_at=refunded_at,
        )

    # --------------------------------------------------------------- customers

    async def list_customers(self) -> AsyncIterator[Customer]:
        async for item in self._paginate("/customers"):
            yield Customer(
                provider="stripe",
                provider_customer_id=str(item.get("id") or ""),
                email=(item.get("email") or "").lower(),
                name=item.get("name"),
                country=(item.get("address") or {}).get("country"),
                created_at=_ts_to_dt(item.get("created")),
            )

    # ----------------------------------------------------------- subscriptions

    async def list_subscriptions(self) -> AsyncIterator[Subscription]:
        # Expand customer + items.data.price so we can compute MRR without
        # per-subscription extra GETs.
        params = {
            "expand[]": ["data.customer", "data.items.data.price"],
            "status": "all",  # include trialing/past_due/canceled in one walk
        }
        async for sub in self._paginate("/subscriptions", params=params):
            raw_status = (sub.get("status") or "").lower()
            status = _STRIPE_SUB_STATUS_MAP.get(raw_status, "active")
            customer = sub.get("customer") or {}
            customer_email = (
                customer.get("email", "").lower() if isinstance(customer, dict) else ""
            )
            items = ((sub.get("items") or {}).get("data") or [])
            first_item = items[0] if items else {}
            price = first_item.get("price") or {}
            recurring = price.get("recurring") or {}
            unit_cents = int(price.get("unit_amount") or 0)
            interval = (recurring.get("interval") or "month").lower()
            interval_count = int(recurring.get("interval_count") or 1)
            # E.g. interval=year, interval_count=1 → months=12.
            #      interval=month, interval_count=3 → months=3 (quarterly billing).
            months = _RECURRING_TO_MONTHS.get(interval, 1) * interval_count
            monthly_cents = unit_cents // months if (unit_cents and months) else 0
            currency = (price.get("currency") or sub.get("currency") or "usd").upper()
            product_name = price.get("nickname") or (
                (sub.get("metadata") or {}).get("product_name")
            )
            yield Subscription(
                provider="stripe",
                provider_subscription_id=str(sub.get("id") or ""),
                customer_email=customer_email,
                status=status,
                monthly_recurring=Money(amount_cents=monthly_cents, currency=currency),
                product_name=product_name,
                started_at=_ts_to_dt(sub.get("start_date") or sub.get("created"))
                or datetime.now(timezone.utc),
                renews_at=_ts_to_dt(sub.get("current_period_end")),
                cancelled_at=_ts_to_dt(sub.get("canceled_at") or sub.get("ended_at")),
            )

    # ------------------------------------------------------------- healthcheck

    async def healthcheck(self) -> bool:
        """GET /v1/balance — cheapest authenticated call, no list pagination."""
        try:
            resp = await self._client.get("/balance")
            return resp.status_code == 200
        except Exception:
            return False
