from __future__ import annotations

from abc import ABC, abstractmethod

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Literal
from datetime import datetime
from TradingBot.domain.orders import TimeInForce
from TradingBot.domain.types import IntentId, StrategyId, Symbol


class IntentPayloadABC(ABC):
    # The @property decorator lets us access the method like an attribute (object.primary_symbol, vs. object.primary_symbol() ).
    # This:
    # - Protects our API from future change, if we later decide to add some code to the primary_symbol method
    @abstractmethod
    def primary_symbol(self) -> Symbol:
        """
        Primary symbol for dedupe and exposure checks.

        Examples
        - PMCC: underlying_symbol
        - Single option: underlying_symbol
        - Equity: symbol
        """
        raise NotImplementedError

class PositionIntent(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_OPEN = "sell_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_CLOSE = "sell_to_close"


LegRole = Literal["LEAP", "NEAR", "OTHER"]


@dataclass(frozen=True)
class IntentOptionLeg:
    contract_symbol: str
    position_intent: PositionIntent
    role: LegRole
    qty: Optional[int] = None


class OrderSide(Enum):
    """
    Direction of an order leg.

    Design intent
    - Encodes intent structure (buy vs sell), not permission.
    - Used by risk evaluation and later broker order translation.
    """

    BUY = "BUY"
    SELL = "SELL"

@dataclass(frozen=True)
class SelectedOption:
    option_symbol: str
    ask_price: float
    bid_price: float
    delta: float
    dte: int

    # Option chain metadata (broker-agnostic)
    feed: str = "indicative"  # "opra" or "indicative"
    chain_newest_ts_utc: Optional[datetime] = None

@dataclass(frozen=True)
class OptionLeg:
    """
    One leg of a multi-leg options intent.

    Design intent
    - Encodes trade structure, not approval.
    - ratio expresses structural relationship between legs, not final sizing.
    """

    contract: SelectedOption
    side: OrderSide
    ratio: int = 1


@dataclass(frozen=True)
class PmccIntentPayload(IntentPayloadABC):
    """
    Payload for a Poor Man's Covered Call (PMCC).

    High-level structure
    - BUY a long-dated call option (LEAP).
    - SELL a shorter-dated call option (near-term) against that LEAP.
    """
    underlying_symbol: Symbol
    leap_leg: OptionLeg
    near_leg: OptionLeg

    @property
    def primary_symbol(self) -> Symbol:
        return self.underlying_symbol



@dataclass(frozen=True)
class EquityIntentPayload(IntentPayloadABC):
    """
    Payload for equity-style intents (stocks and ETFs).

    Use cases
    - BUY SPY shares
    - SELL AAPL shares

    Notes
    - quantity is intentionally not here if your risk engine sizes.
      If you later want strategies to request a quantity, add
      requested_quantity and let risk cap/override it.
    """
    symbol: Symbol
    side: OrderSide

    @property
    def primary_symbol(self) -> Symbol:
        return self.symbol


@dataclass(frozen=True)
class SingleOptionIntentPayload(IntentPayloadABC):
    """
    Payload for a single-leg option intent (if you ever need it).

    Example
    - BUY one call option
    - SELL one put option
    """
    underlying_symbol: Symbol
    leg: OptionLeg

    @property
    def primary_symbol(self) -> Symbol:
        return self.underlying_symbol


@dataclass(frozen=True)
class OptionIntentPayload(IntentPayloadABC):
    underlying_symbol: Symbol
    leg: IntentOptionLeg

    @property
    def primary_symbol(self) -> Symbol:
        return self.underlying_symbol


@dataclass(frozen=True)
class MultiLegOptionIntentPayload(IntentPayloadABC):
    underlying_symbol: Symbol
    legs: Tuple[IntentOptionLeg, ...]

    @property
    def primary_symbol(self) -> Symbol:
        return self.underlying_symbol


@dataclass(frozen=True)
class TradeIntent:
    """
    A request from a strategy to the risk engine.

    Design intent
    - Expresses desire, not permission.
    - The only object strategies are allowed to emit.

    Explicit exclusions
    - No broker order object.
    - No side effects.
    - No final quantity if sizing is risk-layer responsibility.
    """
    intent_id: IntentId
    strategy_id: StrategyId
    symbol: Symbol
    payload: IntentPayloadABC
    time_in_force: TimeInForce = TimeInForce.DAY
    tags: Tuple[str, ...] = ()
