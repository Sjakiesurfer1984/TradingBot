from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from TradingBot.v2.context import RiskContext
from TradingBot.v2.domain.ids import make_intent_id
from TradingBot.v2.domain.types import IntentId, StrategyId, Symbol, normalise_symbol
from TradingBot.v2.intents import PmccIntentPayload, SelectedOption, TradeIntent
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2

from TradingBot.v2.logger import setup_logger
logger = setup_logger("PMCC Strategy")

@dataclass(frozen=True)
class PmccStrategyV2(StrategyV2):
    underlying_symbol: str
    _strategy_id: StrategyId = StrategyId("pmcc_v2")

    @property
    def strategy_id(self) -> StrategyId:
        return self._strategy_id

    @property
    def symbols(self) -> Sequence[Symbol]:
        # Orchestrator will prefetch these.
        return (normalise_symbol(self.underlying_symbol),)

    def generate_intents(self, ctx: RiskContext) -> List[TradeIntent]:
        underlying: Symbol = normalise_symbol(self.underlying_symbol)

        price = ctx.get_price(underlying)
        if price is None:
            return []

        intent_id: IntentId = make_intent_id(
            strategy_id=self.strategy_id,
            underlying=underlying,
            as_of_utc=ctx.as_of_utc,
        )

        placeholder_leap: SelectedOption = SelectedOption(
            option_symbol=f"{underlying}_LEAP_PLACEHOLDER",
            ask_price=0.0,
            bid_price=0.0,
            delta=0.0,
            dte=0,
        )

        placeholder_near: SelectedOption = SelectedOption(
            option_symbol=f"{underlying}_NEAR_PLACEHOLDER",
            ask_price=0.0,
            bid_price=0.0,
            delta=0.0,
            dte=0,
        )

        payload: PmccIntentPayload = PmccIntentPayload(
            underlying_symbol=underlying,
            leap=placeholder_leap,
            near=placeholder_near,
        )

        tags: Tuple[str, ...] = ("v2", "pmcc", "placeholder")

        intent: TradeIntent = TradeIntent(
            intent_id=intent_id,
            strategy_id=self.strategy_id,
            symbol=underlying,
            payload=payload,
            time_in_force="day",
            tags=tags,
        )

        return [intent]
