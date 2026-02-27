from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict

from src.risk.risk_engine import RiskEngine
from src.signals.signal_pipeline import SignalPipelineABC
from src.strategies.interfaces import StrategyABC


class StrategyPluginABC(ABC):

    @abstractmethod
    def build(self, spec) -> StrategyABC:
        raise NotImplementedError

    @abstractmethod
    def register_evaluators(self, spec, engine: RiskEngine) -> None:
        raise NotImplementedError

    @abstractmethod
    def build_signal_pipeline(self, spec, broker) -> SignalPipelineABC:
        raise NotImplementedError


class PmccStrategyPlugin(StrategyPluginABC):

    def build(self, spec) -> StrategyABC:
        from src.strategies.pmcc_strategy import PmccStrategy
        return PmccStrategy(config=spec.pmcc)

    def register_evaluators(self, spec, engine: RiskEngine) -> None:
        from src.risk.evaluators import PmccEvaluator
        from src.risk.pmcc_sizer import PmccSizer, PmccSizingConfig

        r          = spec.pmcc.risk
        sizing_cfg = PmccSizingConfig(
            max_buying_power_fraction=r.max_buying_power_fraction,
            max_debit_per_spread_usd=r.max_debit_per_spread_usd,
            max_contracts_per_intent=r.max_contracts_per_intent,
            slippage_factor=r.slippage_factor,
        )
        evaluator = PmccEvaluator(
            sizer=PmccSizer(config=sizing_cfg),
            max_units=spec.pmcc.max_units,
        )
        # One evaluator registered under the strategy's id.
        # The evaluator handles all PMCC payload types internally.
        engine.register("pmcc", evaluator)

    def build_signal_pipeline(self, spec, broker) -> SignalPipelineABC:
        from src.signals.signal_pipeline import IvRegimePipelineWithBars
        return IvRegimePipelineWithBars(
            underlying=spec.underlying_symbol,
            broker=broker,
        )


# ---------------------------------------------------------------------------
# GoF Registry
#
# To add Wheel:
#   1. Create WheelStrategyPlugin(StrategyPluginABC)
#   2. Add: "wheel": WheelStrategyPlugin()
#   Nothing else changes anywhere.
# ---------------------------------------------------------------------------

STRATEGY_PLUGIN_REGISTRY: Dict[str, StrategyPluginABC] = {
    "pmcc": PmccStrategyPlugin(),
}