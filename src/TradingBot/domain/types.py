from __future__ import annotations

from dataclasses import dataclass
from typing import NewType, Optional
from datetime import date, datetime

from TradingBot.v2.logger import setup_logger
logger = setup_logger("Types")


# NewType creates a distinct type at type-check time, while remaining a str at runtime.
# This helps static checkers catch mistakes like passing a StrategyId where an IntentId is expected.
IntentId = NewType("IntentId", str)
StrategyId = NewType("StrategyId", str)
ClientOrderId = NewType("ClientOrderId", str)
Symbol = NewType("Symbol", str)


def normalise_symbol(value: str) -> Symbol:
    """
    Convert an arbitrary string into a normalised Symbol.

    Why this exists
    - Symbols come from config files, user input, or strategy code.
    - Normalising once prevents subtle bugs caused by whitespace or casing differences.

    What we enforce
    - Must be a non-empty string after stripping whitespace.
    - Stored as uppercase for consistent dictionary keys and logging.
    """
    if not isinstance(value, str):
        raise TypeError("Symbol must be a string.")
    cleaned: str = value.strip().upper()
    if not cleaned:
        raise ValueError("Symbol must be a non-empty string.")
    return Symbol(cleaned)


@dataclass(frozen=True)
class AllocationFraction:
    """
    A fractional allocation in the closed interval [0.0, 1.0].

    Why this exists
    - Risk and orchestration frequently allocate capital by percentage or fraction.
    - Using a dedicated type prevents ambiguity between:
        - 0.08 meaning "8 percent"
        - 8.0 meaning "8 percent"
    - This forces you to represent allocations consistently as fractions.

    Example
    - AllocationFraction(0.08) means 8 percent of a budget.
    """

    value: float

    def __post_init__(self) -> None:
        # dataclass(frozen=True) makes the instance immutable after creation,
        # but __post_init__ still runs during construction, so we can validate here.
        if not isinstance(self.value, (int, float)):
            raise TypeError("AllocationFraction.value must be numeric.")
        if float(self.value) < 0.0 or float(self.value) > 1.0:
            raise ValueError("AllocationFraction.value must be in [0.0, 1.0].")

    def as_float(self) -> float:
        """
        Return the allocation fraction as a float.

        Why this exists
        - Keeps a simple and explicit conversion point for arithmetic.
        """
        return float(self.value)


@dataclass(frozen=True)
class Money:
    """
    A minimal money value represented in a single currency.

    Why this exists
    - Many parts of a trading system manipulate cash amounts:
        - equity
        - buying power
        - risk budgets
    - Representing money explicitly reduces confusion when passing values around.

    What this does NOT do
    - It does not implement multi-currency conversion.
    - It does not implement decimal precision rules yet.
      For now, floats are acceptable for the early architecture stage.

    Later
    - If you want strict accounting, we can switch value to Decimal.
    """

    value: float
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float)):
            raise TypeError("Money.value must be numeric.")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("Money.currency must be a non-empty string.")

    def as_float(self) -> float:
        """
        Return the money value as a float.

        Why this exists
        - Keeps arithmetic usage explicit rather than relying on implicit float casting.
        """
        return float(self.value)

@dataclass(frozen=True, slots=True)
class AssetQuote:
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    timestamp_utc: Optional[datetime]


@dataclass(frozen=True)
class OptionChainRequest:
    """
    Request for an option chain snapshot for a single underlying.

    Why this exists
    - Strategies are not allowed to call the broker.
    - Strategies can express what data they need (constraints + filters).
    - The orchestrator collects these requests and performs broker IO once per cycle.

    Field groups
    - Routing: underlying
    - Instrument scope: include_calls/include_puts
    - Data freshness: feed, max_age_seconds
    - Payload size: limit
    - API filters: strike and expiration filters, root_symbol, updated_since
    """

    # Required
    underlying: str
    request_id: str
    
    # Call/put selection (at least one must be True)
    include_calls: bool = True
    include_puts: bool = False

    # Data feed selection (broker validates supported values)
    feed: str = "indicative"

    # Optional client-side cap after pagination (0 means "no limit")
    limit: int = 0

    # Alpaca option chain filters (all optional)
    strike_price_gte: Optional[float] = None
    strike_price_lte: Optional[float] = None

    expiration_date: Optional[date] = None
    expiration_date_gte: Optional[date] = None
    expiration_date_lte: Optional[date] = None

    root_symbol: Optional[str] = None

    # Fetch only contracts updated since a point in time (UTC recommended)
    updated_since: Optional[datetime] = None

