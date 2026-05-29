"""Offline shape test for the Polar adapter.

No Polar account is set up yet, so this is fixture-only. Live smoke goes into a
follow-up script once Petro decides whether to register a Polar org.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

sys.path.insert(0, "src")

from indie_founder_mcp.adapters.polar import PolarAdapter  # noqa: E402


def _make_transport(
    responses: dict[tuple[str, str], dict[str, Any]],
) -> httpx.MockTransport:
    """Keyed by (method, path) → JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Adapter sets base_url to .../v1; absolute path comes through as /v1/...
        key_path = path[3:] if path.startswith("/v1") else path
        body = responses.get((request.method, key_path))
        if body is None:
            return httpx.Response(
                404,
                json={"error": f"no fixture for ({request.method}, {key_path})"},
            )
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


ORDERS_PAGE_1 = {
    "items": [
        {
            "id": "ord_001",
            "status": "paid",
            "paid": True,
            "net_amount": 4900,
            "total_amount": 4900,
            "tax_amount": 0,
            "currency": "usd",
            "customer": {"id": "cust_001", "email": "buyer1@example.com"},
            "items": [{"product": {"id": "prod_a", "name": "Indie Founder Toolkit"}}],
            "billing_reason": "purchase",
            "created_at": "2026-05-20T14:32:11Z",
        },
        {
            "id": "ord_002",
            "status": "refunded",
            "paid": True,
            "net_amount": 1900,
            "total_amount": 1900,
            "currency": "usd",
            "customer": {"id": "cust_002", "email": "buyer2@example.com"},
            "items": [{"product": {"id": "prod_b", "name": "Founder Cheatsheet"}}],
            "billing_reason": "purchase",
            "created_at": "2026-05-18T09:00:00Z",
        },
    ],
    "pagination": {"total_count": 2, "max_page": 1},
}

SUBSCRIPTIONS_PAGE_1 = {
    "items": [
        {
            "id": "sub_monthly",
            "status": "active",
            "amount": 1900,
            "currency": "usd",
            "recurring_interval": "month",
            "customer": {"id": "cust_001", "email": "buyer1@example.com"},
            "product": {"id": "prod_a", "name": "Pro Monthly"},
            "started_at": "2026-05-01T00:00:00Z",
            "current_period_end": "2026-06-01T00:00:00Z",
            "cancel_at_period_end": False,
            "canceled_at": None,
            "ends_at": None,
            "ended_at": None,
        },
        {
            "id": "sub_yearly",
            "status": "trialing",
            "amount": 19000,
            "currency": "usd",
            "recurring_interval": "year",
            "customer": {"id": "cust_002", "email": "buyer2@example.com"},
            "product": {"id": "prod_b", "name": "Pro Yearly"},
            "started_at": "2026-05-15T00:00:00Z",
            "current_period_end": "2027-05-15T00:00:00Z",
            "cancel_at_period_end": False,
        },
        {
            "id": "sub_cancelled",
            "status": "canceled",
            "amount": 1900,
            "currency": "usd",
            "recurring_interval": "month",
            "customer": {"id": "cust_003", "email": "buyer3@example.com"},
            "product": {"id": "prod_a", "name": "Pro Monthly"},
            "started_at": "2026-03-01T00:00:00Z",
            "current_period_end": "2026-05-01T00:00:00Z",
            "cancel_at_period_end": True,
            "canceled_at": "2026-04-20T10:00:00Z",
            "ends_at": "2026-05-01T00:00:00Z",
        },
    ],
    "pagination": {"total_count": 3, "max_page": 1},
}

CUSTOMERS_PAGE_1 = {
    "items": [
        {
            "id": "cust_001",
            "email": "buyer1@example.com",
            "name": "Buyer One",
            "created_at": "2026-05-01T00:00:00Z",
            "external_id": None,
        }
    ],
    "pagination": {"total_count": 1, "max_page": 1},
}


# ------------------------------- Tests --------------------------------------


def make_adapter(transport: httpx.MockTransport) -> tuple[PolarAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url="https://api.polar.sh/v1", transport=transport)
    return PolarAdapter(token="fake", client=client), client


async def test_list_orders_maps_paid_and_refunded() -> None:
    transport = _make_transport({("GET", "/orders/"): ORDERS_PAGE_1})
    adapter, client = make_adapter(transport)
    try:
        orders = [o async for o in adapter.list_orders()]
    finally:
        await client.aclose()
    by_id = {o.provider_order_id: o for o in orders}
    assert len(orders) == 2
    paid = by_id["ord_001"]
    assert paid.status == "paid"
    assert paid.gross.amount_cents == 4900
    assert paid.gross.currency == "USD"
    assert paid.customer_email == "buyer1@example.com"
    assert paid.product_name == "Indie Founder Toolkit"
    refunded = by_id["ord_002"]
    assert refunded.status == "refunded"


async def test_list_orders_filters_window_clientside() -> None:
    transport = _make_transport({("GET", "/orders/"): ORDERS_PAGE_1})
    adapter, client = make_adapter(transport)
    try:
        since = datetime(2026, 5, 19, tzinfo=timezone.utc)
        orders = [o async for o in adapter.list_orders(since=since)]
    finally:
        await client.aclose()
    assert [o.provider_order_id for o in orders] == ["ord_001"]


async def test_list_subscriptions_status_and_mrr() -> None:
    transport = _make_transport(
        {("GET", "/subscriptions/"): SUBSCRIPTIONS_PAGE_1}
    )
    adapter, client = make_adapter(transport)
    try:
        subs = [s async for s in adapter.list_subscriptions()]
    finally:
        await client.aclose()
    by_id = {s.provider_subscription_id: s for s in subs}
    assert len(subs) == 3
    # Monthly $19/mo
    assert by_id["sub_monthly"].status == "active"
    assert by_id["sub_monthly"].monthly_recurring.amount_cents == 1900
    # Yearly $190 → MRR = 19000 / 12 = 1583
    assert by_id["sub_yearly"].status == "trialing"
    assert by_id["sub_yearly"].monthly_recurring.amount_cents == 19000 // 12
    # Cancelled → mapped to "cancelled" (Polar uses single-l "canceled")
    assert by_id["sub_cancelled"].status == "cancelled"
    assert by_id["sub_cancelled"].cancelled_at is not None


async def test_list_customers() -> None:
    transport = _make_transport(
        {("GET", "/customers/"): CUSTOMERS_PAGE_1}
    )
    adapter, client = make_adapter(transport)
    try:
        customers = [c async for c in adapter.list_customers()]
    finally:
        await client.aclose()
    assert len(customers) == 1
    assert customers[0].email == "buyer1@example.com"
    assert customers[0].name == "Buyer One"


async def test_healthcheck() -> None:
    transport = _make_transport(
        {("GET", "/subscriptions/"): SUBSCRIPTIONS_PAGE_1}
    )
    adapter, client = make_adapter(transport)
    try:
        ok = await adapter.healthcheck()
    finally:
        await client.aclose()
    assert ok is True


async def test_pagination_walks_multiple_pages() -> None:
    page1 = {
        "items": [
            {
                "id": f"ord_{i}",
                "status": "paid",
                "paid": True,
                "net_amount": 100 * i,
                "currency": "usd",
                "customer": {"id": "c", "email": f"b{i}@example.com"},
                "items": [],
                "created_at": "2026-05-20T00:00:00Z",
            }
            for i in range(1, 3)
        ],
        "pagination": {"total_count": 4, "max_page": 2},
    }
    page2 = {
        "items": [
            {
                "id": f"ord_{i}",
                "status": "paid",
                "paid": True,
                "net_amount": 100 * i,
                "currency": "usd",
                "customer": {"id": "c", "email": f"b{i}@example.com"},
                "items": [],
                "created_at": "2026-05-20T00:00:00Z",
            }
            for i in range(3, 5)
        ],
        "pagination": {"total_count": 4, "max_page": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        page = int(params.get("page", "1"))
        body = page1 if page == 1 else page2
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    adapter, client = make_adapter(transport)
    try:
        orders = [o async for o in adapter.list_orders()]
    finally:
        await client.aclose()
    assert [o.provider_order_id for o in orders] == ["ord_1", "ord_2", "ord_3", "ord_4"]


async def main() -> None:
    tests = [
        test_list_orders_maps_paid_and_refunded,
        test_list_orders_filters_window_clientside,
        test_list_subscriptions_status_and_mrr,
        test_list_customers,
        test_healthcheck,
        test_pagination_walks_multiple_pages,
    ]
    failed = 0
    for t in tests:
        try:
            await t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        sys.exit(1)
    print(f"\nall {len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
