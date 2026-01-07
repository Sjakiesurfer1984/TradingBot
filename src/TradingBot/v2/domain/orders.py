# src/TradingBot/v2/domain/orders.py

from __future__ import annotations

# dataclass is used to define small immutable value objects with minimal boilerplate.
from dataclasses import dataclass

# Enum provides named constants. This prevents "magic strings" like "buy" or "sell"
# being typed differently in different places (e.g., "BUY", "Buy", "buy").
from enum import Enum

# Decimal is used for money-like quantities where you want predictable arithmetic.
# Floats can introduce rounding noise (e.g., 0.1 + 0.2 != 0.3 exactly in binary floating point).
from decimal import Decimal

# datetime is used to represent timestamps.
# Optional describes values that may be missing (None).
# Sequence is for ordered collections (list or tuple).
from datetime import datetime
from typing import Optional, Sequence

# Symbol is a domain type representing a canonicalised underlying symbol.
# We use it here so "SPY", " spy ", and "Spy" get treated as the same canonical thing.
from TradingBot.v2.domain.types import Symbol


class OrderSide(str, Enum):
    """
    Side of a trade.

    Why an Enum
    - It prevents typos and inconsistent wording.
    - It makes function signatures and intent more explicit.

    We inherit from str as well as Enum so the values behave like strings
    when you need to log them or serialize them.
    """

    BUY = "buy"
    SELL = "sell"


class OptionRight(str, Enum):
    """
    Option contract type.

    CALL
    - Right to buy the underlying at the strike.

    PUT
    - Right to sell the underlying at the strike.
    """

    CALL = "call"
    PUT = "put"


class TimeInForce(str, Enum):
    """
    Time-in-force (TIF) instruction.

    DAY
    - Order expires at the end of the trading session.

    GTC
    - Good-till-cancelled, persists until filled or cancelled.

    Why we limit values
    - Brokers support many TIFs.
    - For V2 we keep a small stable set.
    - You can extend later without rewriting the whole architecture.
    """

    DAY = "day"
    GTC = "gtc"


@dataclass(frozen=True)
class OptionContract:
    """
    A broker-agnostic representation of an option contract.

    Design intent
    - This describes an option contract in our domain terms.
    - It is not a broker payload.
    - It contains enough information for:
        - risk rules (exposure reasoning),
        - order construction,
        - logging and traceability.

    Notes
    - option_symbol is allowed because some brokers require a single string identifier.
    - expiry/strike/right are included because they are meaningful domain fields.
    """

    # The underlying equity symbol (e.g., SPY).
    underlying: Symbol

    # Expiration date (UTC naive or aware is fine as long as the system is consistent).
    # For options, only the date portion matters most of the time.
    expiry: datetime

    # Strike price for the option.
    # Decimal is used to avoid floating rounding errors.
    strike: Decimal

    # Whether the option is a call or put.
    right: OptionRight

    # Broker-specific identifier (e.g., OPRA style).
    # This can be None until a broker adapter resolves it.
    option_symbol: Optional[str] = None


@dataclass(frozen=True)
class OptionLeg:
    """
    One leg of a multi-leg options order.

    Design intent
    - Multi-leg orders (like PMCC) are combinations of legs.
    - Each leg has:
        - a contract,
        - a side (buy or sell),
        - a ratio (how many contracts relative to the base quantity).

    Example
    - PMCC typically has:
        - BUY 1 long-dated call (ratio 1)
        - SELL 1 short-dated call (ratio 1)

    Why ratio exists
    - Some spreads use ratios (e.g., 1x2).
    - We need a structured place for this so it does not become a "special case".
    """

    # The option contract for this leg.
    contract: OptionContract

    # Buy or sell direction of this leg.
    side: OrderSide

    # How many contracts of this leg per "unit" of the overall order.
    # For a standard vertical spread, both legs are ratio=1.
    ratio: int = 1


@dataclass(frozen=True)
class MultiLegLimitOrder:
    """
    A broker-agnostic multi-leg limit order.

    Design intent
    - This is the execution-ready order object that can be produced by the risk engine.
    - It must contain:
        - a deterministic client_order_id (for dedupe and tracing),
        - the legs,
        - a quantity (how many "units" of the spread),
        - a limit price,
        - a time in force.

    Why "execution-ready"
    - Once the risk engine produces this order, the orchestrator should be able
      to submit it without changing it.
    - This is part of Option C discipline:
        strategy -> intent (desire)
        risk -> order (approved and sized)
        orchestrator -> submit (IO only)

    Important
    - quantity refers to the number of spread units.
      Each unit multiplies each leg by its ratio.
    """

    # Deterministic identifier set by our system.
    # Used for deduplication, reconciliation, and auditing.
    client_order_id: str

    # Underlying symbol used for reporting, risk grouping, and heuristics.
    underlying: Symbol

    # Order legs in a stable order (e.g., long leg first, short leg second).
    # Sequence allows either list or tuple. We treat it as read-only.
    legs: Sequence[OptionLeg]

    # Number of spread units.
    # Example: quantity=2 with two legs ratio=1 means 2 contracts per leg.
    quantity: int

    # Limit price for the entire multi-leg order.
    # For debit spreads, this is positive (you pay).
    # For credit spreads, you might encode credit as negative or store a separate field.
    # For V2, we keep it as a Decimal and let the broker adapter decide sign convention.
    limit_price: Decimal

    # Time in force instruction.
    time_in_force: TimeInForce = TimeInForce.DAY

    def total_contracts_for_leg(self, leg: OptionLeg) -> int:
        """
        Compute how many contracts this order represents for a specific leg.

        Why this exists
        - Risk calculations often need contract counts.
        - The total contracts depends on overall quantity and leg ratio.

        Example
        - quantity = 3
        - leg.ratio = 2
        -> total contracts for this leg = 6
        """
        return self.quantity * leg.ratio
