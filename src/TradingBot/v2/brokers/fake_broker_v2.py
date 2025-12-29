# ================================
# FILE: src/TradingBot/v2/brokers/fake_broker.py
# ================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from TradingBot.brokers.broker_interface import BrokerInterface


@dataclass
class FakeBroker(BrokerInterface):
    """
    Fake broker for V2 dry-run.

    Why this exists
    - Option C needs the orchestrator, strategies, and risk engine to be testable without network IO.
    - The real Alpaca broker can hang on SSL calls.
    - A fake broker provides deterministic data for fast, safe iteration.

    What this broker does
    - Returns fixed account values (equity, option buying power).
    - Returns fixed prices for symbols.
    - Returns empty positions and empty open orders by default.

    What this broker does not do
    - It does not talk to any API.
    - It does not place orders.
    - It does not attempt to model fills or market microstructure.
    """

    option_buying_power: float = 100_000.0
    equity: float = 100_000.0

    prices: Dict[str, float] = field(default_factory=dict)

    positions: Dict[str, Any] = field(default_factory=dict)
    open_orders: List[Dict[str, Any]] = field(default_factory=list)

    def get_option_buying_power(self) -> float:
        """
        Return a fixed option buying power.

        Why a float
        - Monetary quantities should be represented as numeric types.
        - The orchestrator and risk layer depend on this to apply limits and sizing later.
        """
        return float(self.option_buying_power)

    def get_equity(self) -> float:
        """
        Return a fixed account equity.

        Why this exists
        - Later risk rules will use equity for drawdown and exposure limits.
        """
        return float(self.equity)

    def get_positions(self) -> Dict[str, Any]:
        """
        Return a snapshot of positions.

        Why we return a dict
        - Positions are typically keyed by symbol for quick lookup.
        """
        return dict(self.positions)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return a snapshot of open orders.

        Why we return a list
        - Orders are naturally represented as a collection of records.
        - Deduplication rules scan this list for matching orders.
        """
        return list(self.open_orders)

    def get_asset_price(self, symbol: str) -> float:
        """
        Return a deterministic price for a symbol.

        Behaviour
        - Normalises the symbol to uppercase and strips whitespace.
        - Returns the configured price if present.
        - Raises if missing, so missing price problems are visible.
        """
        key: str = symbol.strip().upper()
        if key not in self.prices:
            raise KeyError(f"FakeBroker has no price configured for symbol: {key}")
        return float(self.prices[key])

    def get_option_chain(self, symbol: str, tipo: str, strike: Any, expiration: Any) -> Any:
        """
        Placeholder for compatibility with the broader interface.

        Why this is here
        - Some V1 code may assume this exists.
        - In V2 Option C, option chain fetching should be orchestrator-owned and explicit.
        """
        raise NotImplementedError("FakeBroker does not implement option chain fetching.")

    def submit_order(self, order: Any) -> Any:
        """
        Placeholder for compatibility.

        Why this raises
        - In V2 dry-run, no submission should occur.
        - If something tries to submit, that is a design violation.
        """
        raise RuntimeError("FakeBroker.submit_order called. Dry-run should not submit orders.")


