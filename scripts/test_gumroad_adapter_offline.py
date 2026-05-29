"""Offline shape test for the Gumroad adapter.

We don't have a live Gumroad access_token wired yet (next-session work — needs
a one-time browser flow to create an app in Settings → Advanced → Applications).
Until we do, this test feeds fixture payloads from the documented Gumroad API
shape and asserts the adapter maps them onto our normalized models correctly.

Real live validation lands in a follow-up smoke script (mirroring the LS one)
once the access_token is in KeePass.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

sys.path.insert(0, "src")

from indie_founder_mcp.adapters.gumroad import GumroadAdapter  # noqa: E402


def _make_transport(responses: dict[tuple[str, str], dict[str, Any]]) -> httpx.MockTransport:
    """`responses` is keyed by (method, path) → JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # The transport sees the resolved absolute URL when base_url is set.
        # /v2/sales is what Gumroad expects; the adapter sets base_url to
        # `.../v2`, so request.url.path includes the /v2 prefix.
        if path.startswith("/v2"):
            key_path = path[3:]  # strip "/v2"
        else:
            key_path = path
        key = (request.method, key_path)
        body = responses.get(key)
        if body is None:
            return httpx.Response(404, json={"success": False, "message": f"no fixture for {key}"})
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


# ----------------------- Fixture: documented Gumroad shapes ------------------

SALES_PAGE_1 = {
    "success": True,
    "sales": [
        {
            "id": "sale_abc",
            "email": "buyer1@example.com",
            "product_id": "prod_recurring",
            "product_name": "Indie Founder Toolkit",
            "price": 4900,
            "gumroad_fee": 593,
            "currency": "usd",
            "refunded": False,
            "disputed": False,
            "dispute_won": False,
            "subscription_id": "sub_aaa",
            "recurrence": "monthly",
            "created_at": "2026-05-20T14:32:11Z",
        },
        {
            "id": "sale_def",
            "email": "buyer2@example.com",
            "product_id": "prod_one_off",
            "product_name": "Founder Cheatsheet",
            "price": 1900,
            "gumroad_fee": 220,
            "currency": "usd",
            "refunded": True,
            "disputed": False,
            "created_at": "2026-05-18T09:00:00Z",
            "updated_at": "2026-05-22T12:00:00Z",
        },
        {
            "id": "sale_ghi",
            "email": "buyer3@example.com",
            "product_id": "prod_eur",
            "product_name": "EU Setup Guide",
            "price": 2500,
            "gumroad_fee": 300,
            "currency": "eur",
            "refunded": False,
            "disputed": True,
            "dispute_won": False,
            "created_at": "2026-05-15T08:00:00Z",
        },
    ],
    "next_page_url": None,
}

PRODUCTS = {
    "success": True,
    "products": [
        {
            "id": "prod_recurring",
            "name": "Indie Founder Toolkit",
            "price": 4900,
            "currency": "usd",
            "recurrences": {"monthly": {}},
        },
        {
            "id": "prod_one_off",
            "name": "Founder Cheatsheet",
            "price": 1900,
            "currency": "usd",
            # No 'recurrences' → not a subscription product.
        },
    ],
}

SUBSCRIBERS_RECURRING = {
    "success": True,
    "subscribers": [
        {
            "id": "sub_aaa",
            "product_id": "prod_recurring",
            "product_name": "Indie Founder Toolkit",
            "user_email": "buyer1@example.com",
            "user_id": "u_111",
            "recurrence": "monthly",
            "status": "alive",
            "created_at": "2026-05-20T14:32:11Z",
        },
        {
            "id": "sub_bbb",
            "product_id": "prod_recurring",
            "product_name": "Indie Founder Toolkit",
            "user_email": "buyer4@example.com",
            "user_id": "u_222",
            "recurrence": "yearly",
            "status": "pending_cancellation",
            "created_at": "2026-04-01T00:00:00Z",
            "cancelled_at": "2026-05-15T00:00:00Z",
        },
        {
            "id": "sub_ccc",
            "product_id": "prod_recurring",
            "product_name": "Indie Founder Toolkit",
            "user_email": "buyer5@example.com",
            "user_id": "u_333",
            "recurrence": "monthly",
            "status": "cancelled",
            "created_at": "2026-03-01T00:00:00Z",
            "ended_at": "2026-04-01T00:00:00Z",
        },
    ],
}

USER_OK = {"success": True, "user": {"email": "me@example.com", "name": "Charlie"}}


# ------------------------------- Tests --------------------------------------


async def test_list_orders_maps_refund_and_dispute() -> None:
    transport = _make_transport(
        {("GET", "/sales"): SALES_PAGE_1, ("GET", "/user"): USER_OK}
    )
    async with httpx.AsyncClient(
        base_url="https://api.gumroad.com/v2", transport=transport
    ) as client:
        adapter = GumroadAdapter(token="fake", client=client)
        orders = [o async for o in adapter.list_orders()]

    assert len(orders) == 3, orders
    by_id = {o.provider_order_id: o for o in orders}

    # Paid subscription sale
    o1 = by_id["sale_abc"]
    assert o1.provider == "gumroad"
    assert o1.status == "paid"
    assert o1.gross.amount_cents == 4900
    assert o1.fee is not None and o1.fee.amount_cents == 593
    assert o1.net is not None and o1.net.amount_cents == 4900 - 593
    assert o1.customer_email == "buyer1@example.com"
    assert o1.created_at == datetime(2026, 5, 20, 14, 32, 11, tzinfo=timezone.utc)
    assert o1.product_name == "Indie Founder Toolkit"

    # Refunded sale
    o2 = by_id["sale_def"]
    assert o2.status == "refunded"
    assert o2.refunded_at == datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)

    # Disputed (open chargeback) — treated as refunded.
    o3 = by_id["sale_ghi"]
    assert o3.status == "refunded"
    assert o3.gross.currency == "EUR"
    assert o3.gross.amount_cents == 2500


async def test_list_orders_filters_window_clientside() -> None:
    transport = _make_transport({("GET", "/sales"): SALES_PAGE_1})
    async with httpx.AsyncClient(
        base_url="https://api.gumroad.com/v2", transport=transport
    ) as client:
        adapter = GumroadAdapter(token="fake", client=client)
        since = datetime(2026, 5, 19, tzinfo=timezone.utc)
        orders = [o async for o in adapter.list_orders(since=since)]
    # Only sale_abc (2026-05-20) is in window; sale_def (05-18) and sale_ghi
    # (05-15) are dropped client-side.
    assert [o.provider_order_id for o in orders] == ["sale_abc"]


async def test_list_subscriptions_normalizes_status_and_mrr() -> None:
    transport = _make_transport(
        {
            ("GET", "/products"): PRODUCTS,
            ("GET", "/products/prod_recurring/subscribers"): SUBSCRIBERS_RECURRING,
        }
    )
    async with httpx.AsyncClient(
        base_url="https://api.gumroad.com/v2", transport=transport
    ) as client:
        adapter = GumroadAdapter(token="fake", client=client)
        subs = [s async for s in adapter.list_subscriptions()]

    by_id = {s.provider_subscription_id: s for s in subs}
    assert len(subs) == 3

    # Monthly alive sub: $49/month → 4900 cents
    s1 = by_id["sub_aaa"]
    assert s1.status == "active"
    assert s1.monthly_recurring.amount_cents == 4900

    # Yearly pending_cancellation → "active" (still has access) → MRR = price / 12.
    s2 = by_id["sub_bbb"]
    assert s2.status == "active"
    assert s2.monthly_recurring.amount_cents == 4900 // 12  # 408 cents

    # Cancelled → "cancelled" status; MRR field still populated but caller
    # should filter on status before summing.
    s3 = by_id["sub_ccc"]
    assert s3.status == "cancelled"
    assert s3.cancelled_at is not None


async def test_list_subscriptions_skips_non_recurring_products() -> None:
    # prod_one_off in PRODUCTS has no `recurrences` — adapter must not hit its
    # /subscribers endpoint (which would 404).
    transport = _make_transport(
        {
            ("GET", "/products"): PRODUCTS,
            ("GET", "/products/prod_recurring/subscribers"): SUBSCRIBERS_RECURRING,
            # Note: /products/prod_one_off/subscribers is intentionally NOT
            # registered; the fixture returns 404 for unknown paths and the
            # test would fail if the adapter called it.
        }
    )
    async with httpx.AsyncClient(
        base_url="https://api.gumroad.com/v2", transport=transport
    ) as client:
        adapter = GumroadAdapter(token="fake", client=client)
        subs = [s async for s in adapter.list_subscriptions()]
    # All 3 subs come from prod_recurring; none from prod_one_off.
    assert len(subs) == 3
    assert {s.product_name for s in subs} == {"Indie Founder Toolkit"}


async def test_list_customers_dedupes_by_email() -> None:
    # buyer1, buyer2, buyer3 → 3 unique emails across the 3 sales fixture.
    transport = _make_transport({("GET", "/sales"): SALES_PAGE_1})
    async with httpx.AsyncClient(
        base_url="https://api.gumroad.com/v2", transport=transport
    ) as client:
        adapter = GumroadAdapter(token="fake", client=client)
        customers = [c async for c in adapter.list_customers()]
    emails = sorted(c.email for c in customers)
    assert emails == ["buyer1@example.com", "buyer2@example.com", "buyer3@example.com"]


async def test_healthcheck() -> None:
    transport = _make_transport({("GET", "/user"): USER_OK})
    async with httpx.AsyncClient(
        base_url="https://api.gumroad.com/v2", transport=transport
    ) as client:
        adapter = GumroadAdapter(token="fake", client=client)
        ok = await adapter.healthcheck()
    assert ok is True


async def main() -> None:
    tests = [
        test_list_orders_maps_refund_and_dispute,
        test_list_orders_filters_window_clientside,
        test_list_subscriptions_normalizes_status_and_mrr,
        test_list_subscriptions_skips_non_recurring_products,
        test_list_customers_dedupes_by_email,
        test_healthcheck,
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
