"""Offline shape test for the Stripe adapter.

No Stripe account on file. This is fixture-only; live smoke is gated on Petro
deciding whether to register Stripe (Stripe atop UA had issues historically —
see TOOLS.md, RULES.md §"no Stripe").
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

sys.path.insert(0, "src")

from indie_founder_mcp.adapters.stripe import StripeAdapter  # noqa: E402


def _make_transport(
    responses: dict[tuple[str, str], dict[str, Any]],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        key_path = path[3:] if path.startswith("/v1") else path
        body = responses.get((request.method, key_path))
        if body is None:
            return httpx.Response(
                404,
                json={"error": {"message": f"no fixture for ({request.method}, {key_path})"}},
            )
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


# Stripe timestamps are Unix seconds. 2026-05-20T14:32:11Z == 1779712331.
TS_2026_05_20 = 1779712331
TS_2026_05_18 = 1779526800
TS_2026_05_01 = 1778025600

CHARGES_PAGE = {
    "object": "list",
    "data": [
        {
            "id": "ch_001",
            "object": "charge",
            "amount": 4900,
            "amount_refunded": 0,
            "currency": "usd",
            "status": "succeeded",
            "refunded": False,
            "description": "Indie Founder Toolkit",
            "billing_details": {"email": "buyer1@example.com"},
            "created": TS_2026_05_20,
        },
        {
            "id": "ch_002",
            "object": "charge",
            "amount": 1900,
            "amount_refunded": 1900,
            "currency": "usd",
            "status": "succeeded",
            "refunded": True,
            "description": "Founder Cheatsheet",
            "billing_details": {"email": "buyer2@example.com"},
            "created": TS_2026_05_18,
            "refunds": {
                "object": "list",
                "data": [{"id": "re_001", "created": TS_2026_05_18 + 86400}],
            },
        },
        {
            "id": "ch_003",
            "object": "charge",
            "amount": 5000,
            "amount_refunded": 1000,  # partial refund
            "currency": "usd",
            "status": "succeeded",
            "refunded": False,
            "billing_details": {"email": "buyer3@example.com"},
            "created": TS_2026_05_20,
        },
    ],
    "has_more": False,
}

SUBSCRIPTIONS_PAGE = {
    "object": "list",
    "data": [
        {
            "id": "sub_monthly",
            "status": "active",
            "currency": "usd",
            "start_date": TS_2026_05_01,
            "current_period_end": TS_2026_05_20 + 30 * 86400,
            "canceled_at": None,
            "ended_at": None,
            "customer": {"id": "cus_001", "email": "buyer1@example.com"},
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_m",
                            "unit_amount": 1900,
                            "currency": "usd",
                            "nickname": "Pro Monthly",
                            "recurring": {"interval": "month", "interval_count": 1},
                        }
                    }
                ]
            },
        },
        {
            "id": "sub_yearly",
            "status": "trialing",
            "currency": "usd",
            "start_date": TS_2026_05_01,
            "current_period_end": TS_2026_05_01 + 365 * 86400,
            "customer": {"id": "cus_002", "email": "buyer2@example.com"},
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_y",
                            "unit_amount": 19000,
                            "currency": "usd",
                            "nickname": "Pro Yearly",
                            "recurring": {"interval": "year", "interval_count": 1},
                        }
                    }
                ]
            },
        },
        {
            "id": "sub_quarterly",
            "status": "canceled",
            "currency": "usd",
            "start_date": TS_2026_05_01,
            "canceled_at": TS_2026_05_20,
            "customer": {"id": "cus_003", "email": "buyer3@example.com"},
            "items": {
                "data": [
                    {
                        "price": {
                            "id": "price_q",
                            "unit_amount": 5700,
                            "currency": "usd",
                            "recurring": {"interval": "month", "interval_count": 3},  # quarterly
                        }
                    }
                ]
            },
        },
    ],
    "has_more": False,
}

CUSTOMERS_PAGE = {
    "object": "list",
    "data": [
        {
            "id": "cus_001",
            "email": "buyer1@example.com",
            "name": "Buyer One",
            "address": {"country": "DE"},
            "created": TS_2026_05_01,
        }
    ],
    "has_more": False,
}

BALANCE_OK = {
    "object": "balance",
    "available": [{"amount": 0, "currency": "usd"}],
}


# ------------------------------- Tests --------------------------------------


def make_adapter(transport: httpx.MockTransport) -> tuple[StripeAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url="https://api.stripe.com/v1", transport=transport)
    return StripeAdapter(api_key="sk_test_fake", client=client), client


async def test_list_orders_maps_paid_refunded_partial() -> None:
    transport = _make_transport({("GET", "/charges"): CHARGES_PAGE})
    adapter, client = make_adapter(transport)
    try:
        orders = [o async for o in adapter.list_orders()]
    finally:
        await client.aclose()
    by_id = {o.provider_order_id: o for o in orders}
    assert len(orders) == 3

    paid = by_id["ch_001"]
    assert paid.status == "paid"
    assert paid.gross.amount_cents == 4900
    assert paid.gross.currency == "USD"
    assert paid.product_name == "Indie Founder Toolkit"
    assert paid.customer_email == "buyer1@example.com"

    refunded = by_id["ch_002"]
    assert refunded.status == "refunded"
    assert refunded.net is not None and refunded.net.amount_cents == 0
    assert refunded.refunded_at == datetime.fromtimestamp(TS_2026_05_18 + 86400, tz=timezone.utc)

    partial = by_id["ch_003"]
    assert partial.status == "partial_refund"
    assert partial.net is not None and partial.net.amount_cents == 5000 - 1000


async def test_list_orders_filters_window_clientside() -> None:
    transport = _make_transport({("GET", "/charges"): CHARGES_PAGE})
    adapter, client = make_adapter(transport)
    try:
        since = datetime.fromtimestamp(TS_2026_05_18 + 86400, tz=timezone.utc)
        orders = [o async for o in adapter.list_orders(since=since)]
    finally:
        await client.aclose()
    # ch_002 (refund created on +86400 day) is exactly equal to `since` — both
    # ch_001 and ch_003 are at TS_2026_05_20 which is > since by ~1d. ch_002
    # itself has created=TS_2026_05_18, which is < since, so it should be dropped.
    ids = sorted(o.provider_order_id for o in orders)
    assert ids == ["ch_001", "ch_003"], ids


async def test_list_subscriptions_status_and_mrr() -> None:
    transport = _make_transport({("GET", "/subscriptions"): SUBSCRIPTIONS_PAGE})
    adapter, client = make_adapter(transport)
    try:
        subs = [s async for s in adapter.list_subscriptions()]
    finally:
        await client.aclose()
    by_id = {s.provider_subscription_id: s for s in subs}
    assert len(subs) == 3

    m = by_id["sub_monthly"]
    assert m.status == "active"
    assert m.monthly_recurring.amount_cents == 1900
    assert m.product_name == "Pro Monthly"

    y = by_id["sub_yearly"]
    assert y.status == "trialing"
    assert y.monthly_recurring.amount_cents == 19000 // 12

    q = by_id["sub_quarterly"]
    assert q.status == "cancelled"
    # 57.00 / 3 months = 19.00/mo
    assert q.monthly_recurring.amount_cents == 5700 // 3


async def test_list_customers() -> None:
    transport = _make_transport({("GET", "/customers"): CUSTOMERS_PAGE})
    adapter, client = make_adapter(transport)
    try:
        customers = [c async for c in adapter.list_customers()]
    finally:
        await client.aclose()
    assert len(customers) == 1
    c = customers[0]
    assert c.email == "buyer1@example.com"
    assert c.country == "DE"


async def test_healthcheck() -> None:
    transport = _make_transport({("GET", "/balance"): BALANCE_OK})
    adapter, client = make_adapter(transport)
    try:
        ok = await adapter.healthcheck()
    finally:
        await client.aclose()
    assert ok is True


async def test_cursor_pagination_walks_pages() -> None:
    page1 = {
        "object": "list",
        "data": [
            {
                "id": f"ch_{i:03d}",
                "object": "charge",
                "amount": 100 * i,
                "currency": "usd",
                "status": "succeeded",
                "refunded": False,
                "billing_details": {"email": f"b{i}@example.com"},
                "created": TS_2026_05_20,
            }
            for i in range(1, 4)
        ],
        "has_more": True,
    }
    page2 = {
        "object": "list",
        "data": [
            {
                "id": f"ch_{i:03d}",
                "object": "charge",
                "amount": 100 * i,
                "currency": "usd",
                "status": "succeeded",
                "refunded": False,
                "billing_details": {"email": f"b{i}@example.com"},
                "created": TS_2026_05_20,
            }
            for i in range(4, 6)
        ],
        "has_more": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        starting_after = request.url.params.get("starting_after")
        body = page2 if starting_after == "ch_003" else page1
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    adapter, client = make_adapter(transport)
    try:
        orders = [o async for o in adapter.list_orders()]
    finally:
        await client.aclose()
    assert [o.provider_order_id for o in orders] == [
        "ch_001",
        "ch_002",
        "ch_003",
        "ch_004",
        "ch_005",
    ]


async def main() -> None:
    tests = [
        test_list_orders_maps_paid_refunded_partial,
        test_list_orders_filters_window_clientside,
        test_list_subscriptions_status_and_mrr,
        test_list_customers,
        test_healthcheck,
        test_cursor_pagination_walks_pages,
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
