from __future__ import annotations

from dataclasses import dataclass
from typing import List

from TradingBot.config.risk_config import RiskConfig
from TradingBot.config.strategy_config import StrategySpec


@dataclass(frozen=True)
class AppRuntimeConfig:
    """
    This is all non-secret configuration that defines what the bot does.
    Secrets remain in .env and are read by your existing settings/env loader.
    """
    strategies: List[StrategySpec]
    risk: RiskConfig
    dry_run_default: bool
