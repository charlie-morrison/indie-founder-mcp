"""Abstract RevenueAdapter contract.

Each provider (LS, Gumroad, Polar, Stripe) ships a concrete subclass that maps
the provider API into the normalized models. The MCP tool layer talks only to
this interface — never to provider SDKs — so adding a provider does not touch
tool code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator

from ..models import Customer, Order, Provider, Subscription


class RevenueAdapter(ABC):
    """One revenue source, normalized."""

    provider: Provider

    @abstractmethod
    async def list_orders(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AsyncIterator[Order]:
        """Yield orders within the window. Implementations must paginate."""
        raise NotImplementedError
        # NOTE: type checkers want `yield` for AsyncIterator subclasses.
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    @abstractmethod
    async def list_customers(self) -> AsyncIterator[Customer]:
        raise NotImplementedError
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    @abstractmethod
    async def list_subscriptions(self) -> AsyncIterator[Subscription]:
        raise NotImplementedError
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    async def healthcheck(self) -> bool:
        """Cheap call to confirm the API token is valid. Default: try one
        order page. Adapters can override with a lighter endpoint."""
        try:
            async for _ in self.list_orders():
                return True
            return True
        except Exception:
            return False
