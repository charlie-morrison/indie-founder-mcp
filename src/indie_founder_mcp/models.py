"""Normalized cross-provider revenue models.

Every adapter (LS, Gumroad, Polar, Stripe) maps its native payload to these
types so MCP tools can sum/sort/group without provider-specific branches.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["lemonsqueezy", "gumroad", "polar", "stripe"]

OrderStatus = Literal["paid", "refunded", "partial_refund", "pending", "failed"]

SubscriptionStatus = Literal[
    "trialing",
    "active",
    "paused",
    "past_due",
    "cancelled",
    "expired",
    "unpaid",
]


class Money(BaseModel):
    """Monetary amount in minor units (cents) + ISO currency code."""

    amount_cents: int
    currency: str = Field(min_length=3, max_length=3)

    @property
    def amount_usd(self) -> Decimal:
        """USD only — non-USD callers should convert before display."""
        if self.currency != "USD":
            raise ValueError(f"Cannot read amount_usd on {self.currency} Money")
        return Decimal(self.amount_cents) / Decimal(100)


class Customer(BaseModel):
    provider: Provider
    provider_customer_id: str
    email: str
    name: str | None = None
    country: str | None = None
    created_at: datetime | None = None


class Order(BaseModel):
    provider: Provider
    provider_order_id: str
    customer_email: str
    status: OrderStatus
    gross: Money
    fee: Money | None = None
    net: Money | None = None
    product_name: str | None = None
    created_at: datetime
    refunded_at: datetime | None = None


class Subscription(BaseModel):
    provider: Provider
    provider_subscription_id: str
    customer_email: str
    status: SubscriptionStatus
    monthly_recurring: Money
    product_name: str | None = None
    started_at: datetime
    renews_at: datetime | None = None
    cancelled_at: datetime | None = None
