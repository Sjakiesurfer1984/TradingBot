from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass(frozen=True)
class PmccLeapConfig:
    dte_min:      int
    dte_max:      int
    target_delta: float


@dataclass(frozen=True)
class PmccShortConfig:
    dte_min:      int
    dte_max:      int
    target_delta: float


@dataclass(frozen=True)
class PmccRollConfig:
    dte_threshold:   int
    delta_threshold: float
    profit_pct:      float


@dataclass(frozen=True)
class PmccLiquidityConfig:
    max_leap_spread_pct: float
    max_near_spread_pct: float


@dataclass(frozen=True)
class PmccRiskConfig:
    equity_budget_pct:        float
    max_option_bp_fraction:   float
    max_debit_per_spread_usd: float
    max_contracts_per_intent: int
    slippage_factor:          float
    ignore_spread_checks:     bool


@dataclass(frozen=True)
class PmccConfig:
    underlying_symbol: str
    max_units:         int
    leap:              PmccLeapConfig
    short:             PmccShortConfig
    roll:              PmccRollConfig
    liquidity:         PmccLiquidityConfig
    risk:              PmccRiskConfig


@dataclass(frozen=True)
class StrategySpec:
    name: str
    pmcc: Optional[PmccConfig] = None


@dataclass(frozen=True)
class GlobalRiskConfig:
    enable_open_order_dedupe: bool = True


@dataclass(frozen=True)
class AppConfig:
    config_path:     Path
    default_dry_run: bool
    cycle_seconds:   float
    risk:            GlobalRiskConfig
    strategies:      List[StrategySpec]


_SEARCH_PATHS = [
    Path("config/config.yaml"),
    Path("config.yaml"),
    Path("../config/config.yaml"),
]


def load_app_config(path: Optional[Path] = None) -> AppConfig:
    if path is None:
        for candidate in _SEARCH_PATHS:
            if candidate.exists():
                path = candidate
                break
    if path is None or not path.exists():
        raise FileNotFoundError(f"config.yaml not found. Searched: {_SEARCH_PATHS}")

    raw = yaml.safe_load(path.read_text())

    risk_raw    = raw.get("risk", {})
    global_risk = GlobalRiskConfig(
        enable_open_order_dedupe=bool(risk_raw.get("enable_open_order_dedupe", True)),
    )

    strategies: List[StrategySpec] = []
    for s in raw.get("strategies", []):
        name = s.get("name")
        pmcc: Optional[PmccConfig] = None
        if name == "pmcc" and "pmcc" in s:
            p    = s["pmcc"]
            pmcc = PmccConfig(
                underlying_symbol=p["underlying_symbol"],
                max_units=int(p["max_units"]),
                leap=PmccLeapConfig(**p["leap"]),
                short=PmccShortConfig(**p["short"]),
                roll=PmccRollConfig(**p["roll"]),
                liquidity=PmccLiquidityConfig(**p["liquidity"]),
                risk=PmccRiskConfig(**p["risk"]),
            )
        strategies.append(StrategySpec(name=name, pmcc=pmcc))

    return AppConfig(
        config_path=path,
        default_dry_run=bool(raw.get("default_dry_run", False)),
        cycle_seconds=float(raw.get("cycle_seconds", 60)),
        risk=global_risk,
        strategies=strategies,
    )
