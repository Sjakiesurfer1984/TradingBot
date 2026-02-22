from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple

from src.domain.orders import OptionContract, TimeInForce
from src.domain.types import IntentId, StrategyId, Symbol


class PositionIntent(str, Enum):
    BUY_TO_OPEN   = "BTO"
    BUY_TO_CLOSE  = "BTC"
    SELL_TO_OPEN  = "STO"
    SELL_TO_CLOSE = "STC"


class IntentPayloadABC(ABC):
    """Marker base — every intent payload type extends this."""


@dataclass(frozen=True)
class SingleOptionLeg:
    contract_symbol:  str
    position_intent:  PositionIntent
    qty:              Optional[int]


@dataclass(frozen=True)
class OptionIntentPayload(IntentPayloadABC):
    """Single-leg management intent (sell NEAR, buy-back NEAR, etc.)."""
    underlying_symbol: Symbol
    leg:               SingleOptionLeg


@dataclass(frozen=True)
class SelectedOption:
    option_symbol: str
    contract:      OptionContract


@dataclass(frozen=True)
class PmccIntentPayload(IntentPayloadABC):
    """PMCC entry intent — carries both legs so the sizer knows which is LEAP/NEAR."""
    underlying_symbol: Symbol
    leap_leg:          SelectedOption
    near_leg:          SelectedOption


def _make_intent_id(strategy_id: str, symbol: str) -> IntentId:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return IntentId(f"{strategy_id}:{symbol}:{ts}")


@dataclass(frozen=True)
class TradeIntent:
    intent_id:     IntentId
    strategy_id:   StrategyId
    symbol:        Symbol
    payload:       IntentPayloadABC
    time_in_force: TimeInForce          = TimeInForce.DAY
    tags:          Tuple[str, ...]      = field(default_factory=tuple)

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
