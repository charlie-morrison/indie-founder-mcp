"""Offline tests for the license validation helper."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx

sys.path.insert(0, "src")

from indie_founder_mcp.licensing import (  # noqa: E402
    LicenseCheck,
    LS_LICENSE_VALIDATE_URL,
    clear_license_cache,
    validate_license,
    validate_license_cached,
)


VALID_RESPONSE: dict[str, Any] = {
    "valid": True,
    "error": None,
    "license_key": {
        "id": 1234,
        "status": "active",
        "key": "12345678-aaaa-bbbb-cccc-ffffffffffff",
        "activation_limit": 3,
        "activation_usage": 1,
        "created_at": "2026-05-01T00:00:00Z",
        "expires_at": None,
        "test_mode": False,
    },
    "meta": {
        "store_id": 344362,
        "order_id": 9999,
        "order_item_id": 1111,
        "product_id": 222,
        "variant_id": 333,
        "customer_email": "buyer@example.com",
    },
}

EXPIRED_RESPONSE: dict[str, Any] = {
    "valid": True,  # LS sometimes returns valid=true even on expired
    "license_key": {"status": "expired", "activation_limit": 1, "activation_usage": 1},
    "meta": {},
}

INVALID_RESPONSE: dict[str, Any] = {
    "valid": False,
    "error": "license_key not found",
    "license_key": None,
    "meta": None,
}


def _transport_for(payload: dict[str, Any], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == LS_LICENSE_VALIDATE_URL and request.method == "POST":
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": "wrong url"})

    return httpx.MockTransport(handler)


async def test_valid_active_license() -> None:
    transport = _transport_for(VALID_RESPONSE)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await validate_license("test-key", client=client)
    assert result.valid is True
    assert result.status == "active"
    assert result.activation_limit == 3
    assert result.customer_email == "buyer@example.com"
    assert result.store_id == 344362


async def test_expired_license_not_honored_even_if_valid_true() -> None:
    """LS returns valid=true with status=expired on some plans — don't honor it."""
    transport = _transport_for(EXPIRED_RESPONSE)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await validate_license("test-key", client=client)
    assert result.valid is False, "expired license should not be treated as valid"
    assert result.status == "expired"


async def test_invalid_license() -> None:
    transport = _transport_for(INVALID_RESPONSE, status=400)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await validate_license("bad-key", client=client)
    assert result.valid is False
    assert result.raw_error == "license_key not found"


async def test_empty_license_key_short_circuits() -> None:
    result = await validate_license("")
    assert result.valid is False
    assert result.status == "missing"


async def test_cache_hits_after_first_call() -> None:
    """Second call within TTL should not hit the network."""
    clear_license_cache()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=VALID_RESPONSE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        r1 = await validate_license_cached("test-key", client=client)
        r2 = await validate_license_cached("test-key", client=client)
    assert call_count["n"] == 1, f"expected 1 call, got {call_count['n']}"
    assert r1.valid and r2.valid


async def test_cache_expires_after_ttl() -> None:
    clear_license_cache()
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=VALID_RESPONSE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await validate_license_cached("test-key", ttl_seconds=0, client=client)
        # Sleep zero — ttl_seconds=0 means the cache entry expires immediately,
        # next call must re-fetch.
        await validate_license_cached("test-key", ttl_seconds=0, client=client)
    assert call_count["n"] == 2


async def main() -> None:
    tests = [
        test_valid_active_license,
        test_expired_license_not_honored_even_if_valid_true,
        test_invalid_license,
        test_empty_license_key_short_circuits,
        test_cache_hits_after_first_call,
        test_cache_expires_after_ttl,
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
