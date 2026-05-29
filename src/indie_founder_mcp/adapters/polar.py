"""Polar.sh adapter.

Polar API specifics that drove the design:

- Auth = `Authorization: Bearer <OAT>` (Organization Access Token).
- Base URL is configurable so callers can point at the sandbox host
  (https://sandbox-api.polar.sh/v1) instead of production.
- Every list endpoint returns `{items: [...], pagination: {total_count, max_page}}`.
  We walk pages 1..max_page using a small helper.
- Amounts on orders/subscriptions are integer cents. `currency` is lowercase ISO
  ("usd") — we uppercase it before constructing `Money`.
- Subscriptions carry `recurring_interval` of "month" or "year". We normalize to
  monthly cents via the divisor below.
- Subscription status enum: active, trialing, past_due, canceled, incomplete
  (the last two appear in real payloads but were not in the docs snippet — we
  cover both defensively).
- Order status enum: pending, paid, refunded.
- Order amount in v2 of Polar's API is `net_amount` (after discounts); we treat
  it as gross for the unified model since fees are not surfaced per-order. The
  `paid: true` boolean is the trustworthy signal that revenue cleared.
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

POLAR_API_BASE = "https://api.polar.sh/v1"

_POLAR_SUB_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "canceled": "cancelled",
    "incomplete": "unpaid",
    "incomplete_expired": "expired",
    "unpaid": "unpaid",
}

_RECURRING_TO_MONTHS: dict[str, int] = {
    "month": 1,
    "year": 12,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _order_status_from_payload(payload: dict[str, Any]) -> OrderStatus:
    raw = (payload.get("status") or "").lower()
    if raw == "refunded":
        return "refunded"
    if raw == "pending":
        return "pending"
    # Polar treats both `status=paid` AND `paid: true` as cleared. Trust the boolean.
    if payload.get("paid"):
        return "paid"
    if raw == "paid":
        return "paid"
    return "failed"


class PolarAdapter(RevenueAdapter):
    """Production Polar.sh adapter."""

    provider = "polar"

    def __init__(
        self,
        token: str,
        *,
        organization_id: str | None = None,
        base_url: str = POLAR_API_BASE,
        client: httpx.AsyncClient | None = None,
        max_pages: int = 50,
        page_size: int = 100,
    ) -> None:
        if not token:
            raise ValueError("PolarAdapter requires a non-empty access token")
        self._token = token
        self._organization_id = organization_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._max_pages = max_pages
        self._page_size = page_size

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "PolarAdapter":
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
        raise RuntimeError(f"Polar GET {path} failed without exception")

    async def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk pages 1..max_page using Polar's page+limit pagination envelope."""
        base_params: dict[str, Any] = dict(params or {})
        base_params.setdefault("limit", self._page_size)
        if self._organization_id and "organization_id" not in base_params:
            base_params["organization_id"] = self._organization_id
        page = 1
        while page <= self._max_pages:
            base_params["page"] = page
            payload = await self._get_with_retry(path, params=dict(base_params))
            items = payload.get("items") or []
            for item in items:
                yield item
            pagination = payload.get("pagination") or {}
            max_page = int(pagination.get("max_page") or 1)
            if page >= max_page:
                return
            page += 1

    # ------------------------------------------------------------------ orders

    async def list_orders(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[Order]:
        async for item in self._paginate("/orders/"):
            order = self._order_from_item(item)
            created = order.created_at
            if since and created < since:
                continue
            if until and created > until:
                continue
            yield order

    def _order_from_item(self, item: dict[str, Any]) -> Order:
        currency = (item.get("currency") or "usd").upper()
        # Prefer net_amount (post-discount, pre-tax) for revenue; fall back to
        # total_amount, then amount (deprecated field).
        gross_cents = int(
            item.get("net_amount")
            if item.get("net_amount") is not None
            else item.get("total_amount") if item.get("total_amount") is not None
            else item.get("amount") or 0
        )
        tax_cents = int(item.get("tax_amount") or 0)
        customer = item.get("customer") or {}
        items_list = item.get("items") or []
        first_product = (items_list[0].get("product") if items_list else None) or item.get("product") or {}
        created = _parse_dt(item.get("created_at")) or datetime.now(timezone.utc)
        return Order(
            provider="polar",
            provider_order_id=str(item.get("id") or ""),
            customer_email=(customer.get("email") or "").lower(),
            status=_order_status_from_payload(item),
            gross=Money(amount_cents=gross_cents, currency=currency),
            fee=None,
            net=Money(amount_cents=max(gross_cents - tax_cents, 0), currency=currency),
            product_name=first_product.get("name"),
            created_at=created,
            refunded_at=None,
        )

    # --------------------------------------------------------------- customers

    async def list_customers(self) -> AsyncIterator[Customer]:
        async for item in self._paginate("/customers/"):
            yield Customer(
                provider="polar",
                provider_customer_id=str(item.get("id") or ""),
                email=(item.get("email") or "").lower(),
                name=item.get("name"),
                country=None,
                created_at=_parse_dt(item.get("created_at")),
            )

    # ----------------------------------------------------------- subscriptions

    async def list_subscriptions(self) -> AsyncIterator[Subscription]:
        async for item in self._paginate("/subscriptions/"):
            raw_status = (item.get("status") or "").lower()
            status = _POLAR_SUB_STATUS_MAP.get(raw_status, "active")
            customer = item.get("customer") or {}
            product = item.get("product") or {}
            currency = (item.get("currency") or "usd").upper()
            interval = (item.get("recurring_interval") or "month").lower()
            months = _RECURRING_TO_MONTHS.get(interval, 1)
            amount_cents = int(item.get("amount") or 0)
            monthly_cents = amount_cents // months if amount_cents else 0
            yield Subscription(
                provider="polar",
                provider_subscription_id=str(item.get("id") or ""),
                customer_email=(customer.get("email") or "").lower(),
                status=status,
                monthly_recurring=Money(amount_cents=monthly_cents, currency=currency),
                product_name=product.get("name"),
                started_at=_parse_dt(item.get("started_at"))
                or datetime.now(timezone.utc),
                renews_at=_parse_dt(item.get("current_period_end")),
                cancelled_at=_parse_dt(
                    item.get("canceled_at") or item.get("ended_at") or item.get("ends_at")
                ),
            )

    # ------------------------------------------------------------- healthcheck

    async def healthcheck(self) -> bool:
        """Cheap call: ask for one subscription. Returns True iff 200 OK."""
        try:
            resp = await self._client.get(
                "/subscriptions/", params={"limit": 1, "page": 1}
            )
            return resp.status_code == 200
        except Exception:
            return False
