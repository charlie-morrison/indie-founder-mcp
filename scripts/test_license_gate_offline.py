"""Offline tests for v0.2 license-gate wiring on premium tools.

Verifies:
- Free tools (summary_mrr, recent_orders, health) work without a license key.
- Paid tools (top_customers, refund_signal, export_csv_for_tax) return
  license_required when the env var is unset, license_invalid when the LS
  validate endpoint says so, and execute normally when validation passes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import patch

import httpx

sys.path.insert(0, "src")

from indie_founder_mcp import licensing  # noqa: E402
from indie_founder_mcp.licensing import LS_LICENSE_VALIDATE_URL, clear_license_cache  # noqa: E402
from indie_founder_mcp.server import (  # noqa: E402
    _FREE_TOOLS,
    _PAID_TOOLS,
    export_csv_for_tax,
    health,
    recent_orders,
    refund_signal,
    summary_mrr,
    top_customers,
)


def _fn(tool: Any) -> Any:
    """FastMCP wraps tool callables — unwrap to call from tests."""
    return getattr(tool, "fn", tool)


VALID_RESPONSE: dict[str, Any] = {
    "valid": True,
    "license_key": {"status": "active", "activation_limit": 1, "activation_usage": 1},
    "meta": {"store_id": 344362, "customer_email": "buyer@example.com"},
}

INVALID_RESPONSE: dict[str, Any] = {
    "valid": False,
    "error": "license_key not found",
    "license_key": None,
    "meta": None,
}


def _patch_validate_transport(payload: dict[str, Any], status: int = 200):
    """Patch httpx.AsyncClient inside licensing to use a MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LS_LICENSE_VALIDATE_URL:
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": "wrong url"})

    real_asyncclient = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_asyncclient(transport=httpx.MockTransport(handler), **kwargs)

    return patch.object(licensing.httpx, "AsyncClient", factory)


async def test_paid_tool_returns_license_required_when_env_unset() -> None:
    clear_license_cache()
    os.environ.pop("INDIE_FOUNDER_MCP_LICENSE_KEY", None)
    for fn, name in (
        (_fn(top_customers), "top_customers"),
        (_fn(refund_signal), "refund_signal"),
    ):
        result = await fn()
        assert result["status"] == "license_required", (
            f"{name}: expected license_required, got {result.get('status')}"
        )
        assert "purchase_url" in result, f"{name}: missing purchase_url"
        assert set(result["paid_tools"]) == set(_PAID_TOOLS)
        assert set(result["free_tools"]) == set(_FREE_TOOLS)
    csv_result = await _fn(export_csv_for_tax)(year=2026, quarter=2)
    assert csv_result["status"] == "license_required"


async def test_paid_tool_returns_license_invalid_for_bad_key() -> None:
    clear_license_cache()
    os.environ["INDIE_FOUNDER_MCP_LICENSE_KEY"] = "bad-key-zzz"
    try:
        with _patch_validate_transport(INVALID_RESPONSE, status=400):
            result = await _fn(top_customers)()
        assert result["status"] == "license_invalid", (
            f"expected license_invalid, got {result.get('status')}"
        )
        assert "raw_error" in result
    finally:
        os.environ.pop("INDIE_FOUNDER_MCP_LICENSE_KEY", None)
        clear_license_cache()


async def test_paid_tool_passes_through_with_valid_license() -> None:
    """Valid license → tool runs (returns no_adapters because we wire none here)."""
    clear_license_cache()
    os.environ["INDIE_FOUNDER_MCP_LICENSE_KEY"] = "good-key-xxx"
    try:
        with _patch_validate_transport(VALID_RESPONSE):
            result = await _fn(top_customers)(limit=5, days=30)
        # No adapters registered in tests → tool falls through to no_adapters path.
        assert result["status"] == "no_adapters", (
            f"expected no_adapters (no adapters wired), got {result.get('status')}"
        )
        assert result["customers"] == []
    finally:
        os.environ.pop("INDIE_FOUNDER_MCP_LICENSE_KEY", None)
        clear_license_cache()


async def test_free_tools_never_check_license() -> None:
    """summary_mrr, recent_orders, health must work with no license env set."""
    clear_license_cache()
    os.environ.pop("INDIE_FOUNDER_MCP_LICENSE_KEY", None)
    for fn, name in (
        (_fn(summary_mrr), "summary_mrr"),
        (_fn(recent_orders), "recent_orders"),
        (_fn(health), "health"),
    ):
        result = await fn()
        # None of the free tools should ever produce license_required.
        assert result.get("status") != "license_required", (
            f"{name}: free tool returned license_required (should never gate)"
        )


async def test_paid_tier_constants_match_expectation() -> None:
    """The split is the v0.2 contract — fail loudly if it drifts."""
    assert set(_FREE_TOOLS) == {"health", "summary_mrr", "recent_orders"}
    assert set(_PAID_TOOLS) == {"top_customers", "refund_signal", "export_csv_for_tax"}
    assert set(_FREE_TOOLS) & set(_PAID_TOOLS) == set()


async def test_export_csv_validates_quarter_before_license_check() -> None:
    """Bad input should raise ValueError before the gate runs — fail fast on misuse."""
    clear_license_cache()
    os.environ.pop("INDIE_FOUNDER_MCP_LICENSE_KEY", None)
    try:
        await _fn(export_csv_for_tax)(year=2026, quarter=5)
    except ValueError:
        pass
    else:
        raise AssertionError("quarter=5 should raise ValueError")


async def main() -> None:
    tests = [
        test_paid_tier_constants_match_expectation,
        test_free_tools_never_check_license,
        test_paid_tool_returns_license_required_when_env_unset,
        test_paid_tool_returns_license_invalid_for_bad_key,
        test_paid_tool_passes_through_with_valid_license,
        test_export_csv_validates_quarter_before_license_check,
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
