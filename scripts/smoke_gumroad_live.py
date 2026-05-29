"""Live smoke test for the Gumroad adapter against the production account.

Mirrors the LS smoke pattern. Pulls access_token from KeePass Notes for the
'Gumroad - Charlie Morrison' entry, exercises healthcheck + list_orders +
list_customers + list_subscriptions, and prints results.

Expected baseline: charliemorrison2228@gmail.com Gumroad account has 0 products
and 0 sales as of 2026-05-27. So all walks should yield zero items cleanly,
and healthcheck should return True.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys

sys.path.insert(0, "src")

# Pull token from KeePass Notes.
out = subprocess.run(
    [
        "python3",
        os.path.expanduser("~/.openclaw/workspace/scripts/keepass_manager.py"),
        "get",
        "Gumroad - Charlie Morrison",
    ],
    capture_output=True,
    text=True,
    check=True,
).stdout
m = re.search(r"access_token:\s*([A-Za-z0-9_\-]{20,})", out)
if not m:
    raise SystemExit(
        "Could not extract access_token from KeePass notes. "
        "Make sure the 'Gumroad - Charlie Morrison' entry has 'access_token: ...' in Notes."
    )
token = m.group(1)

from indie_founder_mcp.adapters.gumroad import GumroadAdapter  # noqa: E402


async def main() -> None:
    async with GumroadAdapter(token=token) as adapter:
        print("--- healthcheck ---")
        ok = await adapter.healthcheck()
        print(f"  ok={ok}")
        assert ok, "healthcheck failed against production"

        print("\n--- list_orders (all-time) ---")
        orders = [o async for o in adapter.list_orders()]
        print(f"  total orders: {len(orders)}")
        for o in orders[:5]:
            print(
                f"    {o.created_at.isoformat()} {o.provider_order_id} "
                f"{o.customer_email} {o.status} {o.gross.amount_cents/100:.2f} {o.gross.currency}"
            )

        print("\n--- list_customers ---")
        customers = [c async for c in adapter.list_customers()]
        print(f"  unique buyer emails: {len(customers)}")
        for c in customers[:5]:
            print(f"    {c.email}")

        print("\n--- list_subscriptions ---")
        subs = [s async for s in adapter.list_subscriptions()]
        print(f"  total subscriptions: {len(subs)}")
        for s in subs[:5]:
            print(
                f"    {s.provider_subscription_id} {s.customer_email} "
                f"{s.status} ${s.monthly_recurring.amount_cents/100:.2f}/mo"
            )

    print("\nLive smoke complete — adapter speaks production Gumroad correctly.")


if __name__ == "__main__":
    asyncio.run(main())
