from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class PmccLegConfig:
    dte_min: int
    dte_max: int
    target_delta: float


@dataclass(frozen=True)
class PmccRollConfig:
    dte_threshold: int
    delta_threshold: float
    profit_pct: float


@dataclass(frozen=True)
class PmccLiquidityConfig:
    max_leap_spread_pct: float
    max_near_spread_pct: float


@dataclass(frozen=True)
class PmccRiskEnvelope:
    equity_budget_pct: float
    max_option_bp_fraction: float
    max_debit_per_spread_usd: float
    max_contracts_per_intent: int
    slippage_factor: float
    ignore_spread_checks: bool


@dataclass(frozen=True)
class PmccConfig:
    underlying_symbol: str
    max_units: int
    leap: PmccLegConfig
    short: PmccLegConfig
    roll: PmccRollConfig
    liquidity: PmccLiquidityConfig
    risk: PmccRiskEnvelope


StrategyName = Literal["pmcc"]


@dataclass(frozen=True)
class StrategySpec:
    name: StrategyName
    pmcc: Optional[PmccConfig] = None
