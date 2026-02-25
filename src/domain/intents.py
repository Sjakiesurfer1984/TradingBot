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
# Semantic payloads — named after what the strategy wants to achieve,
# not after the mechanical shape of the trade.
#
# ExecutionPolicy reads the payload type and decides how many broker orders
# to emit. Strategy and RiskEngine never see OrderABC.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnterPmccPayload(IntentPayloadABC):
    """
    Open a new PMCC spread.

    Roles are explicit fields — the sizer knows which leg is the LEAP
    without inspecting DTE or any PMCC-specific heuristic.

    → ExecutionPolicy emits ONE MultiLegLimitOrder (BTO leap + STO near).
    → Alpaca MLEG supports this: short is covered by the long in the same order.
    """
    underlying_symbol: Symbol
    leap:              SelectedOption
    near:              SelectedOption
    max_debit:         float   # net debit ceiling in points (e.g. 45.00 = $4500/contract)


@dataclass(frozen=True)
class RollNearPayload(IntentPayloadABC):
    """
    Close the current short NEAR and open a new one.

    Both legs are always present — a roll is never a single-leg action.

    → ExecutionPolicy emits TWO sequential MarketOrders: BTC close, then STO open.
    → Alpaca MLEG CANNOT do this: the STO leg would be uncovered at submission
      (the BTC hasn't filled yet), so Alpaca Level 3 rejects it. Sequential
      single-leg orders are the only safe path.

    max_net_debit: the maximum net cost of the roll in points.
      Negative means we collect a credit (the ideal case).
      Positive means we pay a debit (acceptable up to this ceiling).
    """
    underlying_symbol: Symbol
    close:             SelectedOption   # BTC this
    open:              SelectedOption   # STO this
    max_net_debit:     float


@dataclass(frozen=True)
class CloseLegPayload(IntentPayloadABC):
    """
    Close a single option leg (BTC a short NEAR, or STC a long LEAP).

    Used for:
      - NEAR_ONLY illegal state: BTC the naked short immediately.
      - LEAP danger intermediate step: BTC the near before closing the LEAP.
      - Any single-leg management action.

    → ExecutionPolicy emits ONE MarketOrder.
    """
    underlying_symbol: Symbol
    contract:          SelectedOption
    position_intent:   PositionIntent   # BTC or STC
    qty:               int = 1


@dataclass(frozen=True)
class CloseSpreadPayload(IntentPayloadABC):
    """
    Close the entire PMCC spread (both legs together).

    Used when:
      - LEAP danger threshold is hit — close everything atomically.
      - End-of-backtest cleanup.

    Both legs are STC / BTC closes — no uncovered short is created,
    so Alpaca MLEG accepts this as a single order.

    → ExecutionPolicy emits ONE MultiLegLimitOrder (STC near + STC leap).
    → Alpaca MLEG supports this: both legs are closing, no new short opened.
    """
    underlying_symbol: Symbol
    near:              SelectedOption   # BTC (short → close)
    leap:              SelectedOption   # STC (long → close)


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
    time_in_force: TimeInForce        = TimeInForce.DAY
    tags:          Tuple[str, ...]    = field(default_factory=tuple)

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