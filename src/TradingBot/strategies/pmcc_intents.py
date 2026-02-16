from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from TradingBot.domain.ids import make_intent_id
from TradingBot.domain.intents import (
    TradeIntent,
    OptionIntentPayload,
    IntentOptionLeg,
    PmccIntentPayload,
    OptionLeg,
    PositionIntent,
    OrderSide,
    SelectedOption,
)
from TradingBot.domain.orders import TimeInForce
from TradingBot.domain.types import StrategyId, Symbol


@dataclass(frozen=True)
class PmccIntentFactory:
    strategy_id: StrategyId

    def sell_near(
        self,
        *,
        underlying: Symbol,
        contract_symbol: str,
        tif: TimeInForce,
        qty: Optional[int],
        as_of_utc: datetime,
    ) -> TradeIntent:

        intent_id = make_intent_id(
            strategy_id=self.strategy_id,
            underlying=underlying,
            as_of_utc=as_of_utc,
        )

        return TradeIntent(
            intent_id=intent_id,
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=OptionIntentPayload(
                underlying_symbol=underlying,
                leg=IntentOptionLeg(
                    contract_symbol=contract_symbol,
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
        qty: Optional[int],
        as_of_utc: datetime,
    ) -> TradeIntent:

        intent_id = make_intent_id(
            strategy_id=self.strategy_id,
            underlying=underlying,
            as_of_utc=as_of_utc,
        )

        return TradeIntent(
            intent_id=intent_id,
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=OptionIntentPayload(
                underlying_symbol=underlying,
                leg=IntentOptionLeg(
                    contract_symbol=contract_symbol,
                    position_intent=PositionIntent.BUY_TO_CLOSE,
                    role="NEAR",
                    qty=qty,
                ),
            ),
            time_in_force=tif,
            tags=("pmcc", "manage", "buyback_near"),
        )
