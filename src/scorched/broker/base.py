"""Abstract base class for broker adapters."""
from abc import ABC, abstractmethod
from decimal import Decimal


class BrokerAdapter(ABC):
    """Interface that all broker implementations must satisfy.

    Returns dicts rather than broker-specific objects so callers
    never depend on a concrete SDK.
    """

    @abstractmethod
    async def submit_buy(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        """Submit a buy order. Returns fill info dict with keys:
        status, filled_qty, filled_avg_price, symbol, order_id.
        """
        ...

    @abstractmethod
    async def submit_sell(
        self,
        symbol: str,
        qty: Decimal,
        limit_price: Decimal,
        recommendation_id: int | None,
    ) -> dict:
        """Submit a sell order. Returns fill info dict with same keys as submit_buy,
        plus realized_gain, tax_category.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[dict]:
        """Return all open positions as list of dicts with keys:
        symbol, qty, avg_cost_basis, market_value, unrealized_pl.
        """
        ...

    @abstractmethod
    async def get_account(self) -> dict:
        """Return account summary with keys:
        cash, buying_power, equity, status.
        """
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> dict:
        """Return order status dict with keys:
        order_id, status, filled_qty, filled_avg_price.
        """
        ...
