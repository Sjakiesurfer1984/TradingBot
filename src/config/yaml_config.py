from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml


# ===========================================================================
# Strategy config dataclasses
# ===========================================================================

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
    dte_threshold:         int
    profit_pct:            float
    near_strike_proximity: float
    leap_dte_threshold:    int
    leap_strike_danger:    float


@dataclass(frozen=True)
class PmccLiquidityConfig:
    max_leap_spread_pct: float
    max_near_spread_pct: float


@dataclass(frozen=True)
class PmccRiskConfig:
    max_buying_power_fraction: float
    max_debit_per_spread_usd:  float
    max_contracts_per_intent:  int
    slippage_factor:           float
    ignore_spread_checks:      bool


@dataclass(frozen=True)
class PmccConfig:
    underlying_symbol: str
    max_units:         int
    leap:              PmccLeapConfig
    short:             PmccShortConfig
    roll:              PmccRollConfig
    liquidity:         PmccLiquidityConfig
    risk:              PmccRiskConfig


# ===========================================================================
# StrategySpec
# ===========================================================================

@dataclass(frozen=True)
class StrategySpec:
    name:   str
    config: Any

    @property
    def pmcc(self) -> Optional[PmccConfig]:
        return self.config if isinstance(self.config, PmccConfig) else None


# ===========================================================================
# Backtest config
# ===========================================================================

@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float
    start_date:   date
    end_date:     date
    output_dir:   Path
    log_mode:     str   # "progress" | "verbose"


# ===========================================================================
# Strategy config parser registry
# ===========================================================================

StrategyConfigParser = Callable[[Dict[str, Any]], Any]


def _parse_pmcc_config(raw: Dict[str, Any]) -> Optional[PmccConfig]:
    p = raw.get("pmcc")
    if not p:
        return None
    r  = p["risk"]
    ro = p["roll"]
    return PmccConfig(
        underlying_symbol=p["underlying_symbol"],
        max_units=int(p["max_units"]),
        leap=PmccLeapConfig(**p["leap"]),
        short=PmccShortConfig(**p["short"]),
        roll=PmccRollConfig(
            dte_threshold=int(ro["dte_threshold"]),
            profit_pct=float(ro["profit_pct"]),
            near_strike_proximity=float(ro["near_strike_proximity"]),
            leap_dte_threshold=int(ro["leap_dte_threshold"]),
            leap_strike_danger=float(ro["leap_strike_danger"]),
        ),
        liquidity=PmccLiquidityConfig(**p["liquidity"]),
        risk=PmccRiskConfig(
            max_buying_power_fraction=float(r["max_buying_power_fraction"]),
            max_debit_per_spread_usd=float(r["max_debit_per_spread_usd"]),
            max_contracts_per_intent=int(r["max_contracts_per_intent"]),
            slippage_factor=float(r["slippage_factor"]),
            ignore_spread_checks=bool(r.get("ignore_spread_checks", False)),
        ),
    )


_STRATEGY_CONFIG_PARSERS: Dict[str, StrategyConfigParser] = {
    "pmcc": _parse_pmcc_config,
}


# ===========================================================================
# App-level config
# ===========================================================================

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
    backtest:        Optional[BacktestConfig] = None


# ===========================================================================
# Loader
# ===========================================================================

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

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    risk_raw    = raw.get("risk", {})
    global_risk = GlobalRiskConfig(
        enable_open_order_dedupe=bool(risk_raw.get("enable_open_order_dedupe", True)),
    )

    strategies: List[StrategySpec] = []
    for entry in raw.get("strategies", []):
        name   = str(entry.get("name", "")).strip().lower()
        parser = _STRATEGY_CONFIG_PARSERS.get(name)
        if parser is None:
            raise KeyError(
                f"No config parser registered for strategy '{name}'. "
                f"Registered: {list(_STRATEGY_CONFIG_PARSERS)}"
            )
        strategies.append(StrategySpec(name=name, config=parser(entry)))

    bt_raw         = raw.get("backtest") or {}
    _default_start = (date.today() - timedelta(days=365)).isoformat()
    _default_end   = date.today().isoformat()

    backtest_cfg = BacktestConfig(
        initial_cash=float(os.getenv("BACKTEST_INITIAL_CASH") or bt_raw.get("initial_cash", 100_000)),
        start_date=date.fromisoformat(os.getenv("BACKTEST_START_DATE") or bt_raw.get("start_date", _default_start)),
        end_date=date.fromisoformat(os.getenv("BACKTEST_END_DATE") or bt_raw.get("end_date", _default_end)),
        output_dir=Path(os.getenv("BACKTEST_OUTPUT_DIR") or bt_raw.get("output_dir", "backtest_results")),
        log_mode=str(os.getenv("BACKTEST_LOG_MODE") or bt_raw.get("log_mode", "progress")),
    )

    return AppConfig(
        config_path=path,
        default_dry_run=bool(raw.get("default_dry_run", False)),
        cycle_seconds=float(raw.get("cycle_seconds", 60)),
        risk=global_risk,
        strategies=strategies,
        backtest=backtest_cfg,
    )