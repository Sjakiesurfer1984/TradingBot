"""
src/strategies/strategy_plugin.py
-----------------------------------
A StrategyPlugin is a self-contained bundle that owns both:
  - the strategy instance (implements StrategyABC)
  - the risk evaluator registration for that strategy

This is the extension point for new strategies. Adding a new strategy means:
  1. Implement StrategyABC (and optionally OptionChainConsumerABC).
  2. Implement StrategyPlugin for that strategy.
  3. Register the plugin in STRATEGY_PLUGINS in this file.
  4. Add the strategy config parser to yaml_config._STRATEGY_CONFIG_PARSERS.

main.py never needs to change. RiskEngine never needs to change.
No other file needs to change.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import Dict, List, Type

from src.config.yaml_config import StrategySpec
from src.domain.intents import IntentPayloadABC, OptionIntentPayload, PmccIntentPayload
from src.risk.evaluators import IntentEvaluatorABC, OptionIntentEvaluator, PmccIntentEvaluator
from src.risk.pmcc_sizer import PmccSizer, PmccSizingConfig
from src.risk.risk_engine import RiskEngine
from src.strategies.interfaces import StrategyABC
from src.utilities.logger import setup_logger

logger = setup_logger("StrategyPlugin")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class StrategyPlugin(ABC):
    """
    Self-contained strategy bundle.

    Each concrete plugin is responsible for:
      - building its own StrategyABC instance from the StrategySpec
      - registering its own evaluators with the RiskEngine

    Adding a new strategy = adding a new StrategyPlugin subclass here.
    Nothing else in the codebase needs to change.
    """

    @abstractmethod
    def build_strategy(self, spec: StrategySpec) -> StrategyABC:
        """Construct and return the strategy instance."""
        raise NotImplementedError

    @abstractmethod
    def register_evaluators(self, spec: StrategySpec, engine: RiskEngine) -> None:
        """Register all payload evaluators this strategy needs into the engine."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# PMCC plugin
# ---------------------------------------------------------------------------

class PmccStrategyPlugin(StrategyPlugin):
    """
    Plugin for the Poor Man's Covered Call strategy.

    Evaluators registered:
      - PmccIntentPayload  → PmccIntentEvaluator  (entry sizing + unit cap)
      - OptionIntentPayload → OptionIntentEvaluator (single-leg roll/close)
    """

    def build_strategy(self, spec: StrategySpec) -> StrategyABC:
        from src.strategies.pmcc_strategy import PmccStrategy
        return PmccStrategy(config=spec.pmcc)

    def register_evaluators(self, spec: StrategySpec, engine: RiskEngine) -> None:
        pmcc_cfg = spec.pmcc
        if pmcc_cfg is None:
            logger.error(
                "PMCC strategy spec is missing its 'pmcc:' config block. "
                "Required fields under strategies[*].pmcc.risk:\n"
                "  max_buying_power_fraction: <float>  # e.g. 0.50\n"
                "  max_debit_per_spread_usd:  <float>  # e.g. 14000.0\n"
                "  max_contracts_per_intent:  <int>    # e.g. 1\n"
                "  slippage_factor:           <float>  # e.g. 1.05"
            )
            sys.exit(1)

        r = pmcc_cfg.risk
        sizing_cfg = PmccSizingConfig(
            max_buying_power_fraction=r.max_buying_power_fraction,
            max_debit_per_spread_usd=r.max_debit_per_spread_usd,
            max_contracts_per_intent=r.max_contracts_per_intent,
            slippage_factor=r.slippage_factor,
        )

        engine.register(
            PmccIntentPayload,
            PmccIntentEvaluator(
                sizer=PmccSizer(config=sizing_cfg),
                max_units=pmcc_cfg.max_units,
            ),
        )
        # Single-leg management (roll BTC/STO) uses a generic evaluator
        # shared across any strategy that emits OptionIntentPayloads.
        # Only register it once — a second register() call would silently
        # overwrite the first, which is fine but wasteful.
        if OptionIntentPayload not in engine._evaluators:
            engine.register(OptionIntentPayload, OptionIntentEvaluator())


# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------
# To add a new strategy (e.g. Wheel):
#   1. class WheelStrategyPlugin(StrategyPlugin): ...
#   2. Add "wheel": WheelStrategyPlugin() below.
#   3. Add its config parser to yaml_config._STRATEGY_CONFIG_PARSERS.
#   Done. main.py is untouched.
# ---------------------------------------------------------------------------

STRATEGY_PLUGINS: Dict[str, StrategyPlugin] = {
    "pmcc": PmccStrategyPlugin(),
    # "wheel": WheelStrategyPlugin(), # etc. etc.
}