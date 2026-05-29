"""Multi-provider live smoke — proves v0.2's aggregation works across
LS + Gumroad simultaneously (both have live tokens in KeePass).

Wires both adapters into the server, then calls the MCP tool layer (summary_mrr,
top_customers, recent_orders, refund_signal, health) and prints the aggregated
output. This is the v0.2 "multi-source aggregation" claim, validated.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import types


# Stub mcp.server.fastmcp to avoid pulling the SDK on 2GB RAM.
fake_mcp = types.ModuleType("mcp")
fake_server = types.ModuleType("mcp.server")
fake_fastmcp = types.ModuleType("mcp.server.fastmcp")


class _StubFastMCP:
    def __init__(self, *a, **kw):
        pass

    def tool(self, *a, **kw):
        def deco(fn):
            return fn

        return deco

    def run(self, *a, **kw):
        raise SystemExit("stub: run() not used in smoke")


fake_fastmcp.FastMCP = _StubFastMCP
fake_server.fastmcp = fake_fastmcp
fake_mcp.server = fake_server
sys.modules["mcp"] = fake_mcp
sys.modules["mcp.server"] = fake_server
sys.modules["mcp.server.fastmcp"] = fake_fastmcp

sys.path.insert(0, "src")


def _kp(entry: str) -> str:
    return subprocess.run(
        [
            "python3",
            os.path.expanduser("~/.openclaw/workspace/scripts/keepass_manager.py"),
            "get",
            entry,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# Lemon Squeezy token (api_token in Notes).
ls_blob = _kp("Lemon Squeezy")
ls_token = re.search(r"api_token:\s*([A-Za-z0-9._-]+)", ls_blob).group(1)
os.environ["LS_API_TOKEN"] = ls_token
os.environ.setdefault("LS_STORE_ID", "344362")

# Gumroad access_token in Notes.
gum_blob = _kp("Gumroad - Charlie Morrison")
gum_token = re.search(r"access_token:\s*([A-Za-z0-9_\-]{20,})", gum_blob).group(1)
os.environ["GUMROAD_ACCESS_TOKEN"] = gum_token

from indie_founder_mcp import server  # noqa: E402

server._wire_default_adapters()


async def main() -> None:
    print(f"adapters wired: {sorted(server._adapters)}")
    assert "lemonsqueezy" in server._adapters
    assert "gumroad" in server._adapters

    print("\n--- health() ---")
    h = await server.health()
    print(h)
    assert h["adapters"]["lemonsqueezy"] is True
    assert h["adapters"]["gumroad"] is True

    print("\n--- summary_mrr(days=30) ---")
    s = await server.summary_mrr(days=30)
    print(s)
    assert "lemonsqueezy" in s["providers"]
    assert "gumroad" in s["providers"]

    print("\n--- recent_orders(limit=10) ---")
    r = await server.recent_orders(limit=10)
    print(r)
    # Both providers should appear in `providers` even if one has 0 orders.
    assert set(r["providers"]) >= {"lemonsqueezy", "gumroad"}
    # LS dogfood account has the $17 order; Gumroad has 0. So at least 1.
    assert r["orders"], "expected at least the $17 LS order"

    print("\n--- top_customers(limit=5, days=90) ---")
    t = await server.top_customers(limit=5, days=90)
    print(t)
    assert set(t["providers"]) >= {"lemonsqueezy", "gumroad"}

    print("\n--- refund_signal(days=7) ---")
    rs = await server.refund_signal(days=7)
    print(rs)
    assert rs["alert"] is False  # young dogfood store

    print("\nv0.2 multi-provider aggregation: LS + Gumroad both wired, all 5 tools answer.")


if __name__ == "__main__":
    asyncio.run(main())
