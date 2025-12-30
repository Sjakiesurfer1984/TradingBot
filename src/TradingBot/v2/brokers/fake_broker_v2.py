from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2
from TradingBot.v2.logger import setup_logger
logger = setup_logger("Fake Broker")


@dataclass
class FakeBrokerV2(BrokerInterfaceV2):
    """
    Fake broker implementation for V2.

    Why this exists
    - We need a broker that never performs network IO so we can test Option C safely.
    - The orchestrator must be able to build a RiskContext snapshot without calling a real API.
    - Unit tests should run fast, deterministically, and without credentials.

    What this broker models
    - Account values such as equity and option buying power.
    - Open orders and positions as simple in-memory collections.
    - Underlying prices as a symbol -> float mapping.

    What this broker does not model (by design)
    - Real market latency, fills, partial fills, or order routing.
    - Option chain fetching.
    - Any exchange rules.

    Design choices
    - We store data in normal Python collections:
        - dict for prices and positions, because we want fast lookup by symbol.
        - list for open orders, because dedupe rules scan order records.
    - We normalise symbols to uppercase consistently so lookups behave predictably.
    """

    option_buying_power: float = 100_000.0
    equity: float = 100_000.0

    prices: Dict[str, float] = field(default_factory=dict)

    positions: Dict[str, Any] = field(default_factory=dict)
    open_orders: List[Dict[str, Any]] = field(default_factory=list)

    def get_option_buying_power(self) -> float:
        """
        Return the option buying power.

        Why this returns float
        - Risk sizing uses arithmetic, so we want numeric types.
        - Converting to float makes downstream code consistent.
        """
        return float(self.option_buying_power)

    def get_equity(self) -> float:
        """
        Return account equity.

        Why this exists
        - Equity is used for risk controls such as drawdown and exposure limits.
        """
        return float(self.equity)

    def get_positions(self) -> Dict[str, Any]:
        """
        Return positions as a dictionary.

        Why we return a new dict
        - Returning a copy prevents callers from mutating internal state accidentally.
        - This mimics the idea of a snapshot for the current cycle.
        """
        return dict(self.positions)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return open orders as a list.

        Why we return a new list
        - Returning a copy prevents accidental mutation of internal state.
        - Risk rules should treat orders as read-only snapshot data.
        """
        return list(self.open_orders)

    def get_asset_price(self, symbol: str) -> float:
        """
        Return an underlying price for a symbol.

        Why this method raises KeyError if missing
        - If a strategy requests a symbol we did not configure, we want the failure to be obvious.
        - Silent defaults hide problems and lead to "the bot does nothing" behaviour.

        Normalisation
        - strip() removes leading/trailing whitespace.
        - upper() makes the key consistent.
        """
        sym: str = symbol.strip().upper()

        if sym not in self.prices:
            raise KeyError(f"FakeBrokerV2 has no configured price for symbol: {sym}")

        price: float = float(self.prices[sym])

        if price <= 0.0:
            raise ValueError(f"FakeBrokerV2 price for {sym} must be positive, got {price}")

        return price

    def submit_order(self, order: Any) -> Any:
        """
        Submit an order request.

        Why this raises
        - In early Option C development, we default to dry-run.
        - If this method is called, it means some code path attempted real submission.
        - That is unsafe until approval and sizing are implemented.

        Later
        - When the orchestrator supports submission, tests can swap this behaviour.
        """
        raise RuntimeError("FakeBrokerV2.submit_order was called. Dry-run should not submit orders.")
