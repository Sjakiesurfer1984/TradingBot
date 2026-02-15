from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from TradingBot.domain.ids import make_intent_id
from TradingBot.domain.intents import (
    IntentOptionLeg,
    MultiLegOptionIntentPayload,
    OptionIntentPayload,
    OptionLeg,
    OrderSide,
    PmccIntentPayload,
    PositionIntent,
    SelectedOption,
    TradeIntent,
)
from TradingBot.domain.orders import TimeInForce
from TradingBot.domain.types import IntentId, StrategyId, Symbol


@dataclass(frozen=True)
class PmccIntentFactory:
    strategy_id: StrategyId

    def entry_multileg(
        self,
        *,
        underlying: Symbol,
        leap: SelectedOption,
        near: SelectedOption,
        as_of_utc: datetime,
        tif: TimeInForce,
    ) -> TradeIntent:
        intent_id: IntentId = make_intent_id(
            strategy_id=self.strategy_id,
            underlying=underlying,
            as_of_utc=as_of_utc,
        )

        payload = PmccIntentPayload(
            underlying_symbol=underlying,
            leap_leg=OptionLeg(contract=leap, side=OrderSide.BUY, ratio=1),
            near_leg=OptionLeg(contract=near, side=OrderSide.SELL, ratio=1),
        )

        return TradeIntent(
            intent_id=intent_id,
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=payload,
            time_in_force=tif,
            tags=("pmcc", "entry", "mleg"),
        )

    def sell_near(
        self,
        *,
        underlying: Symbol,
        contract_symbol: str,
        tif: TimeInForce,
        qty: Optional[int] = None,
    ) -> TradeIntent:
        return TradeIntent(
            intent_id=IntentId.new(),
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=OptionIntentPayload(
                underlying_symbol=underlying,
                leg=IntentOptionLeg(
                    contract_symbol=str(contract_symbol).strip().upper(),
                    position_intent=PositionIntent.SELL_TO_OPEN,
                    role="NEAR",
                    qty=qty,
                ),
            ),
            time_in_force=tif,
            tags=("pmcc", "manage", "sell_near"),
        )

    def buyback_near(
        self,
        *,
        underlying: Symbol,
        contract_symbol: str,
        tif: TimeInForce,
        qty: Optional[int] = None,
    ) -> TradeIntent:
        return TradeIntent(
            intent_id=IntentId.new(),
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=OptionIntentPayload(
                underlying_symbol=underlying,
                leg=IntentOptionLeg(
                    contract_symbol=str(contract_symbol).strip().upper(),
                    position_intent=PositionIntent.BUY_TO_CLOSE,
                    role="NEAR",
                    qty=qty,
                ),
            ),
            time_in_force=tif,
            tags=("pmcc", "manage", "buyback_near"),
        )

    def roll_near(
        self,
        *,
        underlying: Symbol,
        held_near_symbol: str,
        new_near_symbol: str,
        tif: TimeInForce,
        qty: Optional[int] = None,
    ) -> TradeIntent:
        legs: Tuple[IntentOptionLeg, ...] = (
            IntentOptionLeg(
                contract_symbol=str(held_near_symbol).strip().upper(),
                position_intent=PositionIntent.BUY_TO_CLOSE,
                role="NEAR",
                qty=qty,
            ),
            IntentOptionLeg(
                contract_symbol=str(new_near_symbol).strip().upper(),
                position_intent=PositionIntent.SELL_TO_OPEN,
                role="NEAR",
                qty=qty,
            ),
        )

        return TradeIntent(
            intent_id=IntentId.new(),
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=MultiLegOptionIntentPayload(underlying_symbol=underlying, legs=legs),
            time_in_force=tif,
            tags=("pmcc", "manage", "roll_near"),
        )
