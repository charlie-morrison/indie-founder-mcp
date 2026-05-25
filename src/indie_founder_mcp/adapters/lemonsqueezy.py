"""Lemon Squeezy adapter.

Wraps the LS REST API into the normalized RevenueAdapter interface. Endpoint
+ pagination patterns mirror the production Projects/tg-bots-saas/scripts/
bill.py (same auth, vnd.api+json content type, links.next pagination).
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

LS_API_BASE = "https://api.lemonsqueezy.com/v1"

_LS_SUB_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "on_trial": "trialing",
    "active": "active",
    "paused": "paused",
    "past_due": "past_due",
    "unpaid": "unpaid",
    "cancelled": "cancelled",
    "expired": "expired",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # LS returns RFC3339 with trailing Z.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _order_status(attrs: dict[str, Any]) -> OrderStatus:
    if attrs.get("refunded"):
        return "refunded"
    raw = (attrs.get("status") or "").lower()
    if raw in {"paid"}:
        return "paid"
    if raw in {"pending"}:
        return "pending"
    if raw in {"failed", "void"}:
        return "failed"
    return "paid"  # LS only emits a handful of statuses; default permissively.


class LemonSqueezyAdapter(RevenueAdapter):
    """Production LS adapter."""

    provider = "lemonsqueezy"

    def __init__(
        self,
        token: str,
        *,
        store_id: int | str | None = None,
        client: httpx.AsyncClient | None = None,
        max_pages: int = 50,
        page_size: int = 100,
    ) -> None:
        if not token:
            raise ValueError("LemonSqueezyAdapter requires a non-empty API token")
        self._token = token
        self._store_id = str(store_id) if store_id is not None else None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=LS_API_BASE,
            headers={
                "Accept": "application/vnd.api+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._max_pages = max_pages
        self._page_size = page_size

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "LemonSqueezyAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw `data` items across all pages, retrying transient errors."""
        merged = dict(params or {})
        merged.setdefault("page[size]", self._page_size)
        if self._store_id:
            merged.setdefault("filter[store_id]", self._store_id)
        url: str | None = path
        next_params: dict[str, Any] | None = merged
        pages = 0
        while url and pages < self._max_pages:
            payload = await self._get_with_retry(url, params=next_params)
            for item in payload.get("data", []) or []:
                yield item
            url = (payload.get("links") or {}).get("next")
            # `links.next` is absolute, so don't reapply params for follow-up pages.
            next_params = None
            pages += 1

    async def _get_with_retry(
        self, url: str, params: dict[str, Any] | None = None, attempts: int = 3
    ) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                # 5xx are transient; 4xx are real errors → fail fast.
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
        raise RuntimeError(f"LS GET {url} failed without exception")

    # ------------------------------------------------------------------ orders

    async def list_orders(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[Order]:
        # LS /orders rejects `filter[updated_at_gte]` and `sort` params (400 in
        # prod). Default order is created_at desc; filter the window client-side.
        async for item in self._paginate("/orders"):
            order = self._order_from_item(item)
            created = order.created_at
            if since and created and created < since:
                continue
            if until and created and created > until:
                continue
            yield order

    def _order_from_item(self, item: dict[str, Any]) -> Order:
        attrs = item.get("attributes") or {}
        currency = (attrs.get("currency") or "USD").upper()
        gross = Money(amount_cents=int(attrs.get("total") or 0), currency=currency)
        tax_cents = int(attrs.get("tax") or 0)
        fee_cents = int(attrs.get("subtotal_formatted_for_card") or 0)  # placeholder
        # LS exposes `total` (gross) and `subtotal` (pre-tax); fee/net are computed
        # by the LS dashboard, not returned per-order. We surface gross only and
        # leave fee/net None — adapter consumers infer fees from settlement.
        del tax_cents, fee_cents
        created = _parse_dt(attrs.get("created_at")) or datetime.now(timezone.utc)
        refunded = _parse_dt(attrs.get("refunded_at"))
        first_item = attrs.get("first_order_item") or {}
        return Order(
            provider="lemonsqueezy",
            provider_order_id=str(item.get("id")),
            customer_email=(attrs.get("user_email") or "").lower(),
            status=_order_status(attrs),
            gross=gross,
            fee=None,
            net=None,
            product_name=first_item.get("product_name"),
            created_at=created,
            refunded_at=refunded,
        )

    # --------------------------------------------------------------- customers

    async def list_customers(self) -> AsyncIterator[Customer]:
        async for item in self._paginate("/customers"):
            attrs = item.get("attributes") or {}
            yield Customer(
                provider="lemonsqueezy",
                provider_customer_id=str(item.get("id")),
                email=(attrs.get("email") or "").lower(),
                name=attrs.get("name"),
                country=attrs.get("country"),
                created_at=_parse_dt(attrs.get("created_at")),
            )

    # ----------------------------------------------------------- subscriptions

    async def list_subscriptions(self) -> AsyncIterator[Subscription]:
        async for item in self._paginate("/subscriptions"):
            attrs = item.get("attributes") or {}
            status = _LS_SUB_STATUS_MAP.get(attrs.get("status") or "", "active")
            # LS subscriptions don't carry an explicit monthly amount on the
            # base resource. Pull it from the embedded variant price if present;
            # otherwise zero so summary_mrr can still rank ratios without crashing.
            unit_price_cents = int(
                attrs.get("first_subscription_item", {}).get("price_id_unit_price", 0)
                or attrs.get("unit_price")
                or 0
            )
            currency = (attrs.get("currency") or "USD").upper()
            yield Subscription(
                provider="lemonsqueezy",
                provider_subscription_id=str(item.get("id")),
                customer_email=(attrs.get("user_email") or "").lower(),
                status=status,
                monthly_recurring=Money(amount_cents=unit_price_cents, currency=currency),
                product_name=attrs.get("product_name"),
                started_at=_parse_dt(attrs.get("created_at")) or datetime.now(timezone.utc),
                renews_at=_parse_dt(attrs.get("renews_at")),
                cancelled_at=_parse_dt(attrs.get("cancelled_at") or attrs.get("ends_at")),
            )

    # ------------------------------------------------------------- healthcheck

    async def healthcheck(self) -> bool:
        """Hit /users/me — cheapest authenticated call."""
        try:
            resp = await self._client.get("/users/me")
            return resp.status_code == 200
        except Exception:
            return False
