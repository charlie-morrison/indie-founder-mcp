"""Lemon Squeezy License-key validation helper.

This module provides the validation primitive that v0.2's "premium" tier will
be built on. It's intentionally NOT wired into any tool yet — the pricing
structure (what's free vs paid) is a product decision pending Petro's input.

Once decided, premium-gated tools call `validate_license_cached(key)` and bail
to a `license_required` response on `valid=False` or missing key.

The validate endpoint requires no Authorization header — the license_key IS
the auth. Endpoint is global Lemon Squeezy (not per-store), so it works for
any store under the same LS account.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

LS_LICENSE_VALIDATE_URL = "https://api.lemonsqueezy.com/v1/licenses/validate"

# License-status enum from LS: "active" | "inactive" | "expired" | "disabled".
_VALID_STATUSES = {"active"}


@dataclass(frozen=True)
class LicenseCheck:
    valid: bool
    status: str  # raw LS status string, or "missing"/"invalid"/"error" sentinels
    activation_limit: int = 0
    activation_usage: int = 0
    expires_at: str | None = None
    customer_email: str | None = None
    store_id: int | None = None
    raw_error: str | None = None


# Process-local cache: license_key -> (LicenseCheck, expires_at_epoch).
# Validate calls cost ~50-200ms; LS rate-limits at 60 req/min on this endpoint;
# license validity doesn't flip every second, so 6 hours is a sane default.
_CACHE: dict[str, tuple[LicenseCheck, float]] = {}
_DEFAULT_TTL_SECONDS = 6 * 3600


async def validate_license(
    license_key: str,
    *,
    instance_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> LicenseCheck:
    """Hit LS /v1/licenses/validate. No caching."""
    if not license_key:
        return LicenseCheck(valid=False, status="missing")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    try:
        form: dict[str, str] = {"license_key": license_key}
        if instance_id:
            form["instance_id"] = instance_id
        try:
            resp = await client.post(
                LS_LICENSE_VALIDATE_URL,
                data=form,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except (httpx.TransportError, httpx.TimeoutException) as e:
            return LicenseCheck(
                valid=False, status="error", raw_error=f"transport: {e}"
            )

        # 4xx with valid=false is the documented invalid-license signal —
        # parse the body before raising.
        try:
            payload: dict[str, Any] = resp.json()
        except ValueError:
            return LicenseCheck(
                valid=False,
                status="error",
                raw_error=f"non-JSON response (HTTP {resp.status_code})",
            )

        return _parse_validate_payload(payload)
    finally:
        if owns_client:
            await client.aclose()


async def validate_license_cached(
    license_key: str,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> LicenseCheck:
    """Cached `validate_license`. Re-checks after `ttl_seconds`."""
    if not license_key:
        return LicenseCheck(valid=False, status="missing")
    cached = _CACHE.get(license_key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    result = await validate_license(license_key, client=client)
    _CACHE[license_key] = (result, now + ttl_seconds)
    return result


def clear_license_cache() -> None:
    """Test seam — clear the process-local validation cache."""
    _CACHE.clear()


def _parse_validate_payload(payload: dict[str, Any]) -> LicenseCheck:
    valid = bool(payload.get("valid"))
    license_key_obj = payload.get("license_key") or {}
    meta = payload.get("meta") or {}
    status = (license_key_obj.get("status") or "").lower()
    # Belt-and-suspenders: if LS says valid=true but the status isn't in our
    # whitelist, treat as invalid (e.g. status="expired" with valid=true is a
    # bug we don't want to honor).
    effective_valid = valid and status in _VALID_STATUSES
    return LicenseCheck(
        valid=effective_valid,
        status=status or ("valid" if valid else "invalid"),
        activation_limit=int(license_key_obj.get("activation_limit") or 0),
        activation_usage=int(license_key_obj.get("activation_usage") or 0),
        expires_at=license_key_obj.get("expires_at"),
        customer_email=meta.get("customer_email"),
        store_id=meta.get("store_id"),
        raw_error=payload.get("error"),
    )


async def main() -> None:  # pragma: no cover — manual smoke
    """Manual smoke: `python -m indie_founder_mcp.licensing <license_key>`."""
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m indie_founder_mcp.licensing <license_key>")
    result = await validate_license(sys.argv[1])
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
