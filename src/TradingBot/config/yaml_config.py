from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml

from TradingBot.config.strategy_config import (
    PmccConfig,
    PmccLegConfig,
    PmccLiquidityConfig,
    PmccRiskEnvelope,
    PmccRollConfig,
    StrategySpec,
)


@dataclass(frozen=True)
class AppRiskConfig:
    enable_open_order_dedupe: bool


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    default_dry_run: bool
    cycle_seconds: float
    risk: AppRiskConfig
    strategies: List[StrategySpec]
    raw: Dict[str, Any]


def load_app_config() -> AppConfig:
    repo_root: Path = Path(__file__).resolve().parents[3]
    config_path: Path = repo_root / "config" / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    default_dry_run: bool = bool(raw.get("default_dry_run", True))
    cycle_seconds: float = float(raw.get("cycle_seconds", 60))

    risk_raw: Dict[str, Any] = raw.get("risk") or {}
    risk = AppRiskConfig(
        enable_open_order_dedupe=bool(risk_raw.get("enable_open_order_dedupe", True)),
    )

    strategies: List[StrategySpec] = []
    for s in (raw.get("strategies") or []):
        name: str = str(s.get("name", "")).strip().lower()
        if name != "pmcc":
            raise ValueError(f"Unsupported strategy name: {name}")

        pmcc_raw: Dict[str, Any] = s.get("pmcc") or {}
        if not pmcc_raw:
            raise ValueError("PMCC strategy requires 'pmcc:' config block")

        pmcc = PmccConfig(
            underlying_symbol=str(pmcc_raw["underlying_symbol"]),
            max_units=int(pmcc_raw["max_units"]),
            leap=PmccLegConfig(
                dte_min=int(pmcc_raw["leap"]["dte_min"]),
                dte_max=int(pmcc_raw["leap"]["dte_max"]),
                target_delta=float(pmcc_raw["leap"]["target_delta"]),
            ),
            short=PmccLegConfig(
                dte_min=int(pmcc_raw["short"]["dte_min"]),
                dte_max=int(pmcc_raw["short"]["dte_max"]),
                target_delta=float(pmcc_raw["short"]["target_delta"]),
            ),
            roll=PmccRollConfig(
                dte_threshold=int(pmcc_raw["roll"]["dte_threshold"]),
                delta_threshold=float(pmcc_raw["roll"]["delta_threshold"]),
                profit_pct=float(pmcc_raw["roll"]["profit_pct"]),
            ),
            liquidity=PmccLiquidityConfig(
                max_leap_spread_pct=float(pmcc_raw["liquidity"]["max_leap_spread_pct"]),
                max_near_spread_pct=float(pmcc_raw["liquidity"]["max_near_spread_pct"]),
            ),
            risk=PmccRiskEnvelope(
                equity_budget_pct=float(pmcc_raw["risk"]["equity_budget_pct"]),
                max_option_bp_fraction=float(pmcc_raw["risk"]["max_option_bp_fraction"]),
                max_debit_per_spread_usd=float(pmcc_raw["risk"]["max_debit_per_spread_usd"]),
                max_contracts_per_intent=int(pmcc_raw["risk"]["max_contracts_per_intent"]),
                slippage_factor=float(pmcc_raw["risk"]["slippage_factor"]),
                ignore_spread_checks=bool(pmcc_raw["risk"]["ignore_spread_checks"]),
            ),
        )

        strategies.append(StrategySpec(name="pmcc", pmcc=pmcc))

    if not strategies:
        raise ValueError(f"No strategies configured in {config_path}")

    return AppConfig(
        config_path=config_path,
        default_dry_run=default_dry_run,
        cycle_seconds=cycle_seconds,
        risk=risk,
        strategies=strategies,
        raw=raw,
    )
