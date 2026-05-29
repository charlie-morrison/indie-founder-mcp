"""Gumroad adapter.

Maps the Gumroad API v2 onto the normalized RevenueAdapter interface.

Gumroad API specifics that drove the design:

- Auth = `Authorization: Bearer <token>` header (also accepts ?access_token=
  query param; header keeps the token out of logs).
- /sales is page-numbered with `next_page_url` / `next_page_key`. Date filter is
  `after` / `before` as `YYYY-MM-DD` (no timezone — applied UTC by Gumroad).
- /sales returns `price` and `gumroad_fee` in cents (despite some community docs
  showing dollars — verified against the published v2 spec).
- There is no /customers endpoint. We derive customers from unique buyer emails
  in /sales.
- Subscriptions are exposed via /products/:product_id/subscribers — a per-product
  list. Subscriber payload doesn't carry a price; we read it from the parent
  product. Recurrence values normalize via _RECURRENCE_MONTHS below.
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

GUMROAD_API_BASE = "https://api.gumroad.com/v2"

# Maps Gumroad recurrence values to month divisor for MRR normalization.
_RECURRENCE_MONTHS: dict[str, int] = {
    "monthly": 1,
    "quarterly": 3,
    "biannually": 6,
    "yearly": 12,
    "every_two_years": 24,
}

# Subscriber.status → normalized SubscriptionStatus.
# Gumroad emits: alive, pending_cancellation, pending_failure, failed_payment,
# fixed_subscription_period_ended, cancelled.
_GUMROAD_SUB_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "alive": "active",
    # pending_cancellation = user cancelled but still in paid period — still
    # generating recognizable revenue this month.
    "pending_cancellation": "active",
    "pending_failure": "past_due",
    "failed_payment": "unpaid",
    "fixed_subscription_period_ended": "expired",
    "cancelled": "cancelled",
}


def _parse_gumroad_dt(value: str | None) -> datetime | None:
    """Gumroad timestamps come back as RFC3339 with trailing Z."""
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _order_status_from_sale(sale: dict[str, Any]) -> OrderStatus:
    if sale.get("refunded"):
        return "refunded"
    # `disputed` without `dispute_won` is an open chargeback — treat as refunded
    # for revenue purposes; if the seller wins the dispute Gumroad will flip
    # `dispute_won` true on the next read.
    if sale.get("disputed") and not sale.get("dispute_won"):
        return "refunded"
    return "paid"


class GumroadAdapter(RevenueAdapter):
    """Production Gumroad adapter."""

    provider = "gumroad"

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_pages: int = 50,
    ) -> None:
        if not token:
            raise ValueError("GumroadAdapter requires a non-empty access token")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=GUMROAD_API_BASE,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._max_pages = max_pages
        self._product_cache: dict[str, dict[str, Any]] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GumroadAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------- HTTP plumbing

    async def _get_with_retry(
        self, url: str, params: dict[str, Any] | None = None, attempts: int = 3
    ) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                if not payload.get("success", True):
                    # Gumroad signals app-layer errors with success=false.
                    raise RuntimeError(
                        f"Gumroad {url} returned success=false: {payload.get('message') or payload}"
                    )
                return payload
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
        raise RuntimeError(f"Gumroad GET {url} failed without exception")

    # ---------------------------------------------------------------- products

    async def _load_products(self) -> dict[str, dict[str, Any]]:
        """Cache the seller's product list keyed by product_id."""
        if self._product_cache is not None:
            return self._product_cache
        payload = await self._get_with_retry("/products")
        products = payload.get("products") or []
        self._product_cache = {str(p.get("id")): p for p in products if p.get("id")}
        return self._product_cache

    # ------------------------------------------------------------------ orders

    async def list_orders(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[Order]:
        # Gumroad /sales accepts `after` and `before` as YYYY-MM-DD (UTC). We
        # pass those to narrow upstream, then re-filter precisely client-side
        # using created_at since Gumroad's date filter is day-granular.
        params: dict[str, Any] = {}
        if since:
            params["after"] = since.astimezone(timezone.utc).date().isoformat()
        if until:
            params["before"] = until.astimezone(timezone.utc).date().isoformat()
        url: str | None = "/sales"
        next_params: dict[str, Any] | None = params
        pages = 0
        while url and pages < self._max_pages:
            payload = await self._get_with_retry(url, params=next_params)
            for sale in payload.get("sales") or []:
                order = self._order_from_sale(sale)
                created = order.created_at
                if since and created < since:
                    continue
                if until and created > until:
                    continue
                yield order
            url = payload.get("next_page_url")
            # next_page_url already encodes the cursor; don't replay filters.
            next_params = None
            pages += 1

    def _order_from_sale(self, sale: dict[str, Any]) -> Order:
        currency = (sale.get("currency") or "USD").upper()
        price_cents = int(sale.get("price") or 0)
        fee_cents = int(sale.get("gumroad_fee") or 0)
        # Net = price - fee. Gumroad doesn't return net directly.
        net_cents = max(price_cents - fee_cents, 0)
        created = _parse_gumroad_dt(sale.get("created_at")) or datetime.now(timezone.utc)
        refunded_at: datetime | None = None
        if sale.get("refunded"):
            # Gumroad doesn't expose refunded_at on the sale payload; the best
            # signal is the most recent updated_at, otherwise leave None.
            refunded_at = _parse_gumroad_dt(sale.get("updated_at"))
        return Order(
            provider="gumroad",
            provider_order_id=str(sale.get("id") or sale.get("order_id") or ""),
            customer_email=(sale.get("email") or "").lower(),
            status=_order_status_from_sale(sale),
            gross=Money(amount_cents=price_cents, currency=currency),
            fee=Money(amount_cents=fee_cents, currency=currency),
            net=Money(amount_cents=net_cents, currency=currency),
            product_name=sale.get("product_name"),
            created_at=created,
            refunded_at=refunded_at,
        )

    # --------------------------------------------------------------- customers

    async def list_customers(self) -> AsyncIterator[Customer]:
        """Derived from unique buyer emails in /sales (no /customers endpoint)."""
        seen: set[str] = set()
        async for order in self.list_orders():
            email = order.customer_email
            if not email or email in seen:
                continue
            seen.add(email)
            yield Customer(
                provider="gumroad",
                provider_customer_id=email,  # stable per-buyer identifier we have
                email=email,
                name=None,
                country=None,
                created_at=order.created_at,
            )

    # ----------------------------------------------------------- subscriptions

    async def list_subscriptions(self) -> AsyncIterator[Subscription]:
        products = await self._load_products()
        for product_id, product in products.items():
            # Skip non-recurring products to avoid 404s on the subscribers endpoint.
            if not product.get("recurrences"):
                continue
            try:
                payload = await self._get_with_retry(
                    f"/products/{product_id}/subscribers"
                )
            except httpx.HTTPStatusError as e:
                # 404 on a product with no subscribers is normal; skip silently.
                if e.response.status_code == 404:
                    continue
                raise
            product_price_cents = int(product.get("price") or 0)
            product_currency = (product.get("currency") or "USD").upper()
            for sub in payload.get("subscribers") or []:
                recurrence = (sub.get("recurrence") or "monthly").lower()
                months = _RECURRENCE_MONTHS.get(recurrence, 1)
                monthly_cents = (
                    product_price_cents // months if product_price_cents else 0
                )
                raw_status = (sub.get("status") or "alive").lower()
                status = _GUMROAD_SUB_STATUS_MAP.get(raw_status, "active")
                yield Subscription(
                    provider="gumroad",
                    provider_subscription_id=str(sub.get("id") or ""),
                    customer_email=(sub.get("user_email") or "").lower(),
                    status=status,
                    monthly_recurring=Money(
                        amount_cents=monthly_cents, currency=product_currency
                    ),
                    product_name=sub.get("product_name") or product.get("name"),
                    started_at=_parse_gumroad_dt(sub.get("created_at"))
                    or datetime.now(timezone.utc),
                    renews_at=None,  # Gumroad doesn't expose next-renewal on subscriber
                    cancelled_at=_parse_gumroad_dt(
                        sub.get("cancelled_at") or sub.get("ended_at")
                    ),
                )

    # ------------------------------------------------------------- healthcheck

    async def healthcheck(self) -> bool:
        """GET /user — cheapest authenticated call, no pagination."""
        try:
            resp = await self._client.get("/user")
            return resp.status_code == 200 and bool(resp.json().get("success"))
        except Exception:
            return False
