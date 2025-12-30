from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal

from TradingBot.v2.logger import setup_logger
logger = setup_logger("Orders")

# ----
# Type aliases
# ----

# Literal restricts values to a fixed set of strings.
# This prevents typos like "BUY " or "selll" at runtime.
OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT"]
TimeInForce = Literal["day", "gtc"]


@dataclass(frozen=True)
class OptionLeg:
    """
    One option leg inside a multi-leg order.

    Why this exists
    - Option strategies (PMCC, spreads, condors) are composed of legs.
    - Each leg has its own contract symbol, side, and quantity.

    Design rules
    - This class is immutable (frozen=True).
    - No broker-specific fields are allowed here.
    - The symbol must be a fully qualified option symbol
      (e.g. 'SPY240621C00500000').
    """

    # Fully qualified option symbol
    symbol: str

    # BUY or SELL
    side: OrderSide

    # Number of contracts (always positive)
    quantity: int


@dataclass(frozen=True)
class MultiLegLimitOrder:
    """
    A broker-agnostic multi-leg option order.

    Why this exists
    - Risk engine produces approved orders in this format.
    - Orchestrator submits this order to a broker adapter.
    - Broker adapters translate this structure into API calls.

    What this class does NOT do
    - It does not submit itself.
    - It does not know anything about Alpaca, IBKR, etc.
    - It does not validate margin or buying power.

    Think of this as:
    - A pure description of trader intent after risk approval.
    """

    # One or more option legs
    legs: List[OptionLeg]

    # Net limit price for the entire order
    # Positive means debit, negative means credit
    limit_price: float

    # Order type is fixed to LIMIT for now
    order_type: OrderType = "LIMIT"

    # Day or good-til-cancelled
    time_in_force: TimeInForce = "day"

    def total_contracts(self) -> int:
        """
        Return the total number of contracts across all legs.

        Why this exists
        - Useful for logging and sanity checks.
        - Keeps this logic close to the domain object.

        Example
        - Two legs, each quantity 1 -> returns 2
        """
        return sum(leg.quantity for leg in self.legs)

    def symbols(self) -> List[str]:
        """
        Return a list of all option symbols used in this order.

        Why this exists
        - Brokers may need to prefetch contracts.
        - Logging and audit trails benefit from this.

        Note
        - Symbols may repeat if the same contract appears twice,
          but that is rare and strategy-dependent.
        """
        return [leg.symbol for leg in self.legs]
