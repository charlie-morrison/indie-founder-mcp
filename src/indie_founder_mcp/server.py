"""FastMCP entrypoint for the Indie Founder Revenue MCP.

v0.0 scaffold — tools return structured placeholders so Claude Desktop can
discover the surface. Wiring to real adapters lands in the ls_adapter_v1 +
mcp_tools_v1 phases.
"""

from __future__ import annotations

import csv
import io
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from . import __version__
from .adapters import RevenueAdapter
from .licensing import validate_license_cached
from .models import Order

load_dotenv()

mcp = FastMCP(
    "indie-founder-revenue",
    instructions=(
        "Aggregated revenue across Lemon Squeezy, Gumroad, Polar, and Stripe. "
        "Use summary_mrr for the headline number, top_customers for ICP work, "
        "refund_signal for retention health, and export_csv_for_tax for the "
        "Ukrainian FOP quarterly filing."
    ),
)

_adapters: dict[str, RevenueAdapter] = {}


def register_adapter(adapter: RevenueAdapter) -> None:
    """Adapter wiring lives in main() so tests can build their own registry."""
    _adapters[adapter.provider] = adapter


def _enabled_providers() -> list[str]:
    return sorted(_adapters.keys())


_ACTIVE_SUB_STATUSES = {"trialing", "active", "past_due"}

_LICENSE_ENV = "INDIE_FOUNDER_MCP_LICENSE_KEY"
_PURCHASE_URL = "https://charliemorrison.lemonsqueezy.com"
_FREE_TOOLS = ("health", "summary_mrr", "recent_orders")
_PAID_TOOLS = ("top_customers", "refund_signal", "export_csv_for_tax")


async def _require_license() -> dict[str, Any] | None:
    """Gate for paid-tier tools. Returns None on valid license, response dict otherwise.

    Free tools (health, summary_mrr, recent_orders) skip this check entirely.
    """
    key = (os.getenv(_LICENSE_ENV) or "").strip()
    if not key:
        return {
            "status": "license_required",
            "version": __version__,
            "message": (
                "This tool is part of the paid tier. Set "
                f"{_LICENSE_ENV} (Lemon Squeezy license key) to enable. "
                f"Buy a key at {_PURCHASE_URL}."
            ),
            "free_tools": list(_FREE_TOOLS),
            "paid_tools": list(_PAID_TOOLS),
            "purchase_url": _PURCHASE_URL,
        }
    check = await validate_license_cached(key)
    if not check.valid:
        return {
            "status": "license_invalid",
            "version": __version__,
            "license_status": check.status,
            "expires_at": check.expires_at,
            "message": (
                f"License key is not valid (status={check.status}). "
                f"Renew or buy a new key at {_PURCHASE_URL}."
            ),
            "raw_error": check.raw_error,
        }
    return None


@mcp.tool()
async def summary_mrr(days: int = 30) -> dict[str, Any]:
    """Combined MRR / ARR across every connected store.

    Sums monthly_recurring across all subscriptions in an active-ish state
    (trialing, active, past_due) for every registered adapter. Paused /
    cancelled / expired subs are excluded.

    Args:
        days: Reserved for future windowed deltas. Currently snapshot-only.
    """
    if not _adapters:
        return {
            "status": "no_adapters",
            "version": __version__,
            "providers": [],
            "mrr_usd": 0.0,
            "arr_usd": 0.0,
        }
    per_provider: dict[str, dict[str, float]] = defaultdict(lambda: {"mrr_usd": 0.0, "active_subs": 0})
    total_mrr_cents = 0
    for name, adapter in _adapters.items():
        try:
            async for sub in adapter.list_subscriptions():
                if sub.status not in _ACTIVE_SUB_STATUSES:
                    continue
                if sub.monthly_recurring.currency != "USD":
                    continue  # mixed-currency MRR not handled in v0.1
                total_mrr_cents += sub.monthly_recurring.amount_cents
                per_provider[name]["mrr_usd"] += sub.monthly_recurring.amount_cents / 100.0
                per_provider[name]["active_subs"] += 1
        except Exception as exc:  # noqa: BLE001
            per_provider[name] = {"error": f"{type(exc).__name__}: {exc}"}
    mrr_usd = total_mrr_cents / 100.0
    return {
        "version": __version__,
        "window_days": days,
        "providers": _enabled_providers(),
        "mrr_usd": round(mrr_usd, 2),
        "arr_usd": round(mrr_usd * 12, 2),
        "per_provider": dict(per_provider),
    }


@mcp.tool()
async def top_customers(limit: int = 10, days: int = 30) -> dict[str, Any]:
    """Highest-revenue customers across all stores in the window.

    Walks paid (non-refunded) orders across every adapter in the last `days`,
    groups by customer email, sums gross USD, ranks descending. Refunds and
    non-USD orders are excluded from the spend total but counted in
    `excluded` for transparency.

    Paid tier — requires INDIE_FOUNDER_MCP_LICENSE_KEY.
    """
    gated = await _require_license()
    if gated is not None:
        return gated
    if not _adapters:
        return {
            "status": "no_adapters",
            "version": __version__,
            "providers": [],
            "customers": [],
        }
    since = datetime.now(timezone.utc) - timedelta(days=days)
    by_email: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "email": "",
            "gross_usd": 0.0,
            "order_count": 0,
            "providers": set(),
            "last_order_at": None,
        }
    )
    excluded = {"refunded": 0, "non_usd": 0, "no_email": 0}
    errors: dict[str, str] = {}
    for name, adapter in _adapters.items():
        try:
            async for order in adapter.list_orders(since=since):
                if order.status in {"refunded", "partial_refund"}:
                    excluded["refunded"] += 1
                    continue
                if order.gross.currency != "USD":
                    excluded["non_usd"] += 1
                    continue
                email = (order.customer_email or "").strip().lower()
                if not email:
                    excluded["no_email"] += 1
                    continue
                row = by_email[email]
                row["email"] = email
                row["gross_usd"] += order.gross.amount_cents / 100.0
                row["order_count"] += 1
                row["providers"].add(order.provider)
                created_iso = order.created_at.isoformat()
                if row["last_order_at"] is None or created_iso > row["last_order_at"]:
                    row["last_order_at"] = created_iso
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"
    ranked = sorted(by_email.values(), key=lambda r: r["gross_usd"], reverse=True)
    customers = [
        {
            "email": r["email"],
            "gross_usd": round(r["gross_usd"], 2),
            "order_count": r["order_count"],
            "providers": sorted(r["providers"]),
            "last_order_at": r["last_order_at"],
        }
        for r in ranked[:limit]
    ]
    return {
        "version": __version__,
        "limit": limit,
        "window_days": days,
        "providers": _enabled_providers(),
        "customers": customers,
        "total_customers": len(by_email),
        "excluded": excluded,
        "errors": errors or None,
    }


@mcp.tool()
async def refund_signal(days: int = 7, alert_multiplier: float = 2.0) -> dict[str, Any]:
    """Flag refund-rate spikes. Compares last N days to the prior N days.

    Pulls orders from the last 2*N days, splits into "current" (most recent N)
    and "prior" (the N days before that). Computes refund_ratio = refunded /
    total for each window. `alert` fires when the current ratio is at least
    `alert_multiplier`x the prior ratio (and the current ratio is non-zero).

    Zero-case: when no refunds in either window, returns alert=False with
    ratios=0.0 — the typical state for a healthy young store.

    Paid tier — requires INDIE_FOUNDER_MCP_LICENSE_KEY.
    """
    gated = await _require_license()
    if gated is not None:
        return gated
    if not _adapters:
        return {
            "status": "no_adapters",
            "version": __version__,
            "providers": [],
            "current_ratio": 0.0,
            "prior_ratio": 0.0,
            "alert": False,
        }
    now = datetime.now(timezone.utc)
    cur_start = now - timedelta(days=days)
    prior_start = now - timedelta(days=2 * days)
    cur = {"total": 0, "refunded": 0, "refunded_usd": 0.0}
    prior = {"total": 0, "refunded": 0, "refunded_usd": 0.0}
    per_provider: dict[str, dict[str, int]] = defaultdict(
        lambda: {"current_total": 0, "current_refunded": 0, "prior_total": 0, "prior_refunded": 0}
    )
    errors: dict[str, str] = {}
    for name, adapter in _adapters.items():
        try:
            async for order in adapter.list_orders(since=prior_start):
                created = order.created_at
                is_refund = order.status in {"refunded", "partial_refund"}
                gross_usd = (
                    order.gross.amount_cents / 100.0 if order.gross.currency == "USD" else 0.0
                )
                if created >= cur_start:
                    cur["total"] += 1
                    per_provider[name]["current_total"] += 1
                    if is_refund:
                        cur["refunded"] += 1
                        cur["refunded_usd"] += gross_usd
                        per_provider[name]["current_refunded"] += 1
                else:
                    prior["total"] += 1
                    per_provider[name]["prior_total"] += 1
                    if is_refund:
                        prior["refunded"] += 1
                        prior["refunded_usd"] += gross_usd
                        per_provider[name]["prior_refunded"] += 1
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"

    def _ratio(window: dict[str, Any]) -> float:
        return (window["refunded"] / window["total"]) if window["total"] else 0.0

    cur_ratio = _ratio(cur)
    prior_ratio = _ratio(prior)
    alert = False
    if cur_ratio > 0:
        if prior_ratio == 0 and cur["refunded"] >= 1 and cur["total"] >= 5:
            alert = True
        elif prior_ratio > 0 and cur_ratio >= alert_multiplier * prior_ratio:
            alert = True
    return {
        "version": __version__,
        "window_days": days,
        "alert_multiplier": alert_multiplier,
        "providers": _enabled_providers(),
        "current": {
            "total_orders": cur["total"],
            "refunded_orders": cur["refunded"],
            "refunded_usd": round(cur["refunded_usd"], 2),
            "ratio": round(cur_ratio, 4),
        },
        "prior": {
            "total_orders": prior["total"],
            "refunded_orders": prior["refunded"],
            "refunded_usd": round(prior["refunded_usd"], 2),
            "ratio": round(prior_ratio, 4),
        },
        "alert": alert,
        "per_provider": {k: dict(v) for k, v in per_provider.items()},
        "errors": errors or None,
    }


@mcp.tool()
async def recent_orders(limit: int = 20) -> dict[str, Any]:
    """Most recent orders across all stores, normalized to one schema."""
    if not _adapters:
        return {
            "status": "no_adapters",
            "version": __version__,
            "providers": [],
            "orders": [],
        }
    since = datetime.now(timezone.utc) - timedelta(days=180)
    collected: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for name, adapter in _adapters.items():
        try:
            async for order in adapter.list_orders(since=since):
                collected.append(
                    {
                        "provider": order.provider,
                        "order_id": order.provider_order_id,
                        "customer_email": order.customer_email,
                        "status": order.status,
                        "gross_usd": (
                            order.gross.amount_cents / 100.0
                            if order.gross.currency == "USD"
                            else None
                        ),
                        "currency": order.gross.currency,
                        "product_name": order.product_name,
                        "created_at": order.created_at.isoformat(),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"
    collected.sort(key=lambda o: o["created_at"], reverse=True)
    return {
        "version": __version__,
        "limit": limit,
        "providers": _enabled_providers(),
        "orders": collected[:limit],
        "errors": errors or None,
    }


_NBU_BASE = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
_nbu_cache: dict[date, float] = {}


async def _fetch_nbu_usd_rate(client: httpx.AsyncClient, on_date: date) -> float:
    """USD/UAH rate from NBU for a given date. Falls back to the next business
    day if NBU returns empty (weekends, holidays).
    """
    if on_date in _nbu_cache:
        return _nbu_cache[on_date]
    probe = on_date
    for _ in range(5):  # walk forward up to 5 days on weekend/holiday
        params = {"valcode": "USD", "date": probe.strftime("%Y%m%d"), "json": ""}
        resp = await client.get(_NBU_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data:
            rate = float(data[0].get("rate") or 0)
            if rate > 0:
                _nbu_cache[on_date] = rate
                return rate
        probe = probe + timedelta(days=1)
    raise RuntimeError(f"NBU returned no USD rate for {on_date} or +5 days")


def _quarter_bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
    start_month = 3 * (quarter - 1) + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if quarter == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


@mcp.tool()
async def export_csv_for_tax(year: int, quarter: int) -> dict[str, Any]:
    """CSV export matching the Ukrainian FOP 3rd-section quarterly report.

    One row per paid order in the quarter, sorted by date ascending.
    Columns: date, provider, order_id, customer_email, product_name,
    gross_usd, fee_usd, net_usd, nbu_rate, gross_uah, net_uah.

    Refunded orders are excluded (FOP 3rd group books revenue on receipt;
    refunds reduce the next reporting period). Non-USD orders are skipped
    and counted in `excluded`. NBU rate fetched from
    bank.gov.ua/NBUStatService for each order's date (weekend dates walk
    forward to the next business-day rate).

    Paid tier — requires INDIE_FOUNDER_MCP_LICENSE_KEY.
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1..4")
    gated = await _require_license()
    if gated is not None:
        return gated
    if not _adapters:
        return {
            "status": "no_adapters",
            "version": __version__,
            "year": year,
            "quarter": quarter,
            "providers": [],
            "csv": "",
            "rows": 0,
        }
    start, end = _quarter_bounds(year, quarter)
    rows: list[Order] = []
    excluded = {"refunded": 0, "non_usd": 0}
    errors: dict[str, str] = {}
    for name, adapter in _adapters.items():
        try:
            async for order in adapter.list_orders(since=start, until=end):
                if order.status in {"refunded", "partial_refund"}:
                    excluded["refunded"] += 1
                    continue
                if order.gross.currency != "USD":
                    excluded["non_usd"] += 1
                    continue
                rows.append(order)
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"

    rows.sort(key=lambda o: o.created_at)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "date",
            "provider",
            "order_id",
            "customer_email",
            "product_name",
            "gross_usd",
            "fee_usd",
            "net_usd",
            "nbu_rate",
            "gross_uah",
            "net_uah",
        ]
    )

    totals = {"gross_usd": 0.0, "fee_usd": 0.0, "net_usd": 0.0, "gross_uah": 0.0, "net_uah": 0.0}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as nbu_client:
        for order in rows:
            d = order.created_at.astimezone(timezone.utc).date()
            try:
                rate = await _fetch_nbu_usd_rate(nbu_client, d)
            except Exception as exc:  # noqa: BLE001
                errors[f"nbu:{d.isoformat()}"] = f"{type(exc).__name__}: {exc}"
                continue
            gross_usd = order.gross.amount_cents / 100.0
            fee_cents = order.fee.amount_cents if order.fee else 0
            fee_usd = fee_cents / 100.0
            net_cents = order.net.amount_cents if order.net else (order.gross.amount_cents - fee_cents)
            net_usd = net_cents / 100.0
            gross_uah = round(gross_usd * rate, 2)
            net_uah = round(net_usd * rate, 2)
            writer.writerow(
                [
                    d.isoformat(),
                    order.provider,
                    order.provider_order_id,
                    order.customer_email,
                    order.product_name or "",
                    f"{gross_usd:.2f}",
                    f"{fee_usd:.2f}",
                    f"{net_usd:.2f}",
                    f"{rate:.4f}",
                    f"{gross_uah:.2f}",
                    f"{net_uah:.2f}",
                ]
            )
            totals["gross_usd"] += gross_usd
            totals["fee_usd"] += fee_usd
            totals["net_usd"] += net_usd
            totals["gross_uah"] += gross_uah
            totals["net_uah"] += net_uah

    return {
        "version": __version__,
        "year": year,
        "quarter": quarter,
        "providers": _enabled_providers(),
        "csv": buf.getvalue(),
        "rows": len(rows),
        "totals": {k: round(v, 2) for k, v in totals.items()},
        "excluded": excluded,
        "errors": errors or None,
    }


@mcp.tool()
async def health() -> dict[str, Any]:
    """Adapter healthcheck — useful before running other tools."""
    results: dict[str, bool | str] = {}
    for name, adapter in _adapters.items():
        try:
            results[name] = await adapter.healthcheck()
        except Exception as exc:  # noqa: BLE001
            results[name] = f"error: {exc.__class__.__name__}: {exc}"
    return {
        "version": __version__,
        "adapters": results or "none configured",
        "license_present": bool(os.getenv("INDIE_FOUNDER_MCP_LICENSE_KEY")),
    }


def _wire_default_adapters() -> None:
    """Read env, register adapters whose tokens are present."""
    ls_token = os.getenv("LS_API_TOKEN")
    if ls_token:
        from .adapters.lemonsqueezy import LemonSqueezyAdapter

        store_id = os.getenv("LS_STORE_ID") or None
        register_adapter(LemonSqueezyAdapter(token=ls_token, store_id=store_id))

    gumroad_token = os.getenv("GUMROAD_ACCESS_TOKEN")
    if gumroad_token:
        from .adapters.gumroad import GumroadAdapter

        register_adapter(GumroadAdapter(token=gumroad_token))

    polar_token = os.getenv("POLAR_API_TOKEN")
    if polar_token:
        from .adapters.polar import PolarAdapter

        org_id = os.getenv("POLAR_ORG_ID") or None
        register_adapter(PolarAdapter(token=polar_token, organization_id=org_id))

    stripe_key = os.getenv("STRIPE_API_KEY")
    if stripe_key:
        from .adapters.stripe import StripeAdapter

        register_adapter(StripeAdapter(api_key=stripe_key))


def main() -> None:
    _wire_default_adapters()
    transport = os.getenv("INDIE_FOUNDER_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in {"streamable-http", "http"}:
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        raise SystemExit(f"unknown transport: {transport}")


if __name__ == "__main__":
    main()
