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
    near_dte_threshold:        int
    near_profit_pct_high_iv:   float
    near_profit_pct_normal_iv: float
    near_profit_pct_low_iv:    float
    near_strike_proximity:     float
    leap_dte_threshold:        int
    leap_strike_danger:        float
    near_max_roll_debit:       float


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
#
# underlying_symbol is declared here — on the spec, not inside PmccConfig —
# because it is a concept shared by all strategy types. main.py reads it
# without ever knowing which concrete strategy config is attached.
#
# asset_class drives calendar selection. Valid: "equity" | "crypto" | "forex".
# Defaults to "equity" so existing configs without the field keep working.
# ===========================================================================

@dataclass(frozen=True)
class StrategySpec:
    name:              str
    asset_class:       str   # "equity" | "crypto" | "forex"
    underlying_symbol: str   # broker-agnostic symbol, e.g. "SPY" or "BTC/USD"
    config:            Any

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
    log_mode:     str


# ===========================================================================
# Strategy config parser registry
#
# OCP: adding a new strategy = one new parser + one registry entry.
#      load_app_config() is never modified.
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
            near_dte_threshold=int(ro["near_dte_threshold"]),
            near_profit_pct_high_iv=float(ro["near_profit_pct_high_iv"]),
            near_profit_pct_normal_iv=float(ro["near_profit_pct_normal_iv"]),
            near_profit_pct_low_iv=float(ro["near_profit_pct_low_iv"]),
            near_strike_proximity=float(ro["near_strike_proximity"]),
            leap_dte_threshold=int(ro["leap_dte_threshold"]),
            leap_strike_danger=float(ro["leap_strike_danger"]),
            near_max_roll_debit=float(ro.get("near_max_roll_debit", 2.0)),
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


def _underlying_from_pmcc(raw: Dict[str, Any]) -> str:
    return str(raw.get("pmcc", {}).get("underlying_symbol", ""))


# Each entry: (config_parser, underlying_extractor)
_STRATEGY_CONFIG_PARSERS: Dict[str, StrategyConfigParser] = {
    "pmcc": _parse_pmcc_config,
}

_STRATEGY_UNDERLYING_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "pmcc": _underlying_from_pmcc,
}


# ===========================================================================
# App-level config
# ===========================================================================

@dataclass(frozen=True)
class GlobalRiskConfig:
    enable_open_order_dedupe: bool = True


@dataclass(frozen=True)
class AppConfig:
    config_path:         Path
    default_dry_run:     bool
    cycle_seconds:       float
    order_check_seconds: float
    risk:                GlobalRiskConfig
    strategies:          List[StrategySpec]
    backtest:            Optional[BacktestConfig] = None


# ===========================================================================
# Loader
# ===========================================================================

_SEARCH_PATHS = [
    Path("config/config.yaml"),
    Path("config.yaml"),
    Path("../config/config.yaml"),
]

_VALID_ASSET_CLASSES = {"equity", "crypto", "forex"}


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
        name        = str(entry.get("name", "")).strip().lower()
        asset_class = str(entry.get("asset_class", "equity")).strip().lower()

        if asset_class not in _VALID_ASSET_CLASSES:
            raise ValueError(
                f"Invalid asset_class '{asset_class}' for strategy '{name}'. "
                f"Valid values: {sorted(_VALID_ASSET_CLASSES)}"
            )

        parser = _STRATEGY_CONFIG_PARSERS.get(name)
        if parser is None:
            raise KeyError(
                f"No config parser registered for strategy '{name}'. "
                f"Registered: {list(_STRATEGY_CONFIG_PARSERS)}"
            )

        underlying_extractor = _STRATEGY_UNDERLYING_EXTRACTORS.get(name)
        if underlying_extractor is None:
            raise KeyError(
                f"No underlying extractor registered for strategy '{name}'."
            )

        underlying = underlying_extractor(entry)
        if not underlying:
            raise ValueError(
                f"Could not extract underlying_symbol for strategy '{name}'."
            )

        strategies.append(StrategySpec(
            name=name,
            asset_class=asset_class,
            underlying_symbol=underlying,
            config=parser(entry),
        ))

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
        cycle_seconds=float(raw.get("cycle_seconds", 300)),
        order_check_seconds=float(raw.get("order_check_seconds", 30)),
        risk=global_risk,
        strategies=strategies,
        backtest=backtest_cfg,
    )