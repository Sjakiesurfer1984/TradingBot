from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Tuple

from src.domain.orders import OptionContract, TimeInForce
from src.domain.types import IntentId, StrategyId, Symbol


class PositionIntent(str, Enum):
    BUY_TO_OPEN   = "BTO"
    BUY_TO_CLOSE  = "BTC"
    SELL_TO_OPEN  = "STO"
    SELL_TO_CLOSE = "STC"


class IntentPayloadABC(ABC):
    """Marker base — every intent payload extends this for RiskEngine dispatch."""


@dataclass(frozen=True)
class SelectedOption:
    option_symbol: str
    contract:      OptionContract


# ---------------------------------------------------------------------------
# OptionLegSpec
#
# Bundles everything about one leg of a trade intent:
#   contract        — which option
#   position_intent — what to do (BTO / BTC / STO / STC)
#   role            — human-readable metadata for logging and DB recording
#                     e.g. "leap", "near", "csp", "cc"
#                     Has zero effect on execution logic — purely informational.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionLegSpec:
    contract:        SelectedOption
    position_intent: PositionIntent
    role:            str  # "leap" | "near" | "csp" | "cc" | etc.


# ---------------------------------------------------------------------------
# Generic payloads
#
# Three types cover every option strategy action:
#
#   SingleLegPayload — one option leg (open or close)
#                      e.g. BTC a short NEAR, STO a CSP, BTO a new LEAP
#
#   SpreadPayload    — two legs executed atomically via MLEG order
#                      e.g. PMCC entry (BTO leap + STO near)
#                           PMCC close spread (BTC near + STC leap)
#                      quantity and limit_price start at 0 (not yet sized).
#                      The evaluator sets them via dataclasses.replace().
#
#   RollPayload      — close one leg then open another sequentially
#                      (MLEG cannot do rolls — the STO would be uncovered
#                      at submission before the BTC fills)
#
# SRP:  each payload describes exactly one category of trade action.
# OCP:  new strategies use the same three types with different roles/intents.
#       No new payload types needed per strategy.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SingleLegPayload(IntentPayloadABC):
    """
    One option leg — open or close.

    → ExecutionPolicy emits ONE MarketOrder.
    """
    underlying_symbol: Symbol
    leg:               OptionLegSpec
    qty:               int = 1


@dataclass(frozen=True)
class SpreadPayload(IntentPayloadABC):
    """
    Two option legs executed atomically via a MultiLeg order.

    Used for:
      - Opening a spread (e.g. PMCC entry: BTO leap + STO near)
      - Closing a spread (e.g. BTC near + STC leap)

    quantity and limit_price are set by the evaluator after sizing.
    Strategy always creates SpreadPayload with quantity=0, limit_price=0.0.
    ExecutionPolicy reads the evaluator-populated values.

    → ExecutionPolicy emits ONE MultiLegLimitOrder.
    """
    underlying_symbol: Symbol
    leg_a:             OptionLegSpec
    leg_b:             OptionLegSpec
    max_debit:         float = 0.0   # net debit ceiling in points
    quantity:          int   = 0     # set by evaluator after sizing
    limit_price:       float = 0.0   # set by evaluator after sizing


@dataclass(frozen=True)
class RollPayload(IntentPayloadABC):
    """
    Close one leg then open another — two sequential MarketOrders.

    max_net_debit: ceiling on (open_ask - close_bid).
    Negative = net credit collected (ideal). Positive = net debit paid.

    → ExecutionPolicy emits TWO sequential MarketOrders.
    """
    underlying_symbol: Symbol
    close:             OptionLegSpec  # position_intent should be BTC or STC
    open_:             OptionLegSpec  # position_intent should be STO or BTO
    max_net_debit:     float = 0.0


# ---------------------------------------------------------------------------
# Envelope + factory
# ---------------------------------------------------------------------------

def _make_intent_id(strategy_id: str, symbol: str) -> IntentId:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return IntentId(f"{strategy_id}:{symbol}:{ts}")


@dataclass(frozen=True)
class TradeIntent:
    intent_id:     IntentId
    strategy_id:   StrategyId
    symbol:        Symbol
    payload:       IntentPayloadABC
    time_in_force: TimeInForce       = TimeInForce.DAY
    tags:          Tuple[str, ...]   = field(default_factory=tuple)

    @classmethod
    def create(
        cls,
        *,
        strategy_id:   StrategyId,
        symbol:        Symbol,
        payload:       IntentPayloadABC,
        time_in_force: TimeInForce = TimeInForce.DAY,
        tags:          Tuple[str, ...] = (),
    ) -> "TradeIntent":
        return cls(
            intent_id=_make_intent_id(str(strategy_id), str(symbol)),
            strategy_id=strategy_id,
            symbol=symbol,
            payload=payload,
            time_in_force=time_in_force,
            tags=tags,
        )