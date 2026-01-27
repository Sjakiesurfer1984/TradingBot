from __future__ import annotations

from dataclasses import dataclass
from typing import List

from TradingBot.v2.config.risk_config import RiskConfig
from TradingBot.v2.config.strategy_config import PmccConfig, StrategySpec


@dataclass(frozen=True)
class RuntimeConfig:
    """
    Non-secret configuration.
    .env remains secrets + toggles only.
    """
    strategies: List[StrategySpec]
    risk: RiskConfig
    default_dry_run: bool = True


def load_runtime_config() -> RuntimeConfig:
    """
    First clean pass: pure Python config.
    Later we can replace this with config.yml.
    """
    return RuntimeConfig(
        default_dry_run=True,
        risk=RiskConfig(enable_open_order_dedupe=True),
        strategies=[
            StrategySpec(
                name="pmcc",
                pmcc=PmccConfig(
                    underlying_symbol="SPY",
                    max_units=2,
                    leap_dte_min=365,
                    leap_dte_max=545,
                    leap_target_delta=0.80,
                    short_dte_min=21,
                    short_dte_max=45,
                    short_target_delta=0.20,
                ),
            )
        ],
    )
