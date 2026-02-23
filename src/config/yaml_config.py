from __future__ import annotations

from dataclasses import dataclass
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
    dte_threshold:   int
    delta_threshold: float
    profit_pct:      float


@dataclass(frozen=True)
class PmccLiquidityConfig:
    max_leap_spread_pct: float
    max_near_spread_pct: float


@dataclass(frozen=True)
class PmccRiskConfig:
    # Maximum fraction of available buying power to use per PMCC entry intent.
    max_buying_power_fraction: float

    # Hard dollar ceiling on net debit per spread regardless of buying power.
    max_debit_per_spread_usd: float

    # Hard contract ceiling per intent — final safety cap after all other checks.
    max_contracts_per_intent: int

    # Multiply estimated cost by this before budget checks (e.g. 1.05 = 5% slippage buffer).
    slippage_factor: float

    # Skip bid-ask spread liquidity checks — only for paper/backtesting diagnostics.
    ignore_spread_checks: bool


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
# StrategySpec — generic container for any strategy config
# ===========================================================================

@dataclass(frozen=True)
class StrategySpec:
    name:   str
    config: Any  # typed config dataclass for the strategy, e.g. PmccConfig

    # Convenience accessor — avoids callers doing isinstance checks everywhere
    @property
    def pmcc(self) -> Optional[PmccConfig]:
        return self.config if isinstance(self.config, PmccConfig) else None


# ===========================================================================
# Strategy config parser registry
# ===========================================================================
# To add a new strategy:
#   1. Define its config dataclass(es) above.
#   2. Write a _parse_<name>_config(raw: dict) -> YourConfig function below.
#   3. Register it in _STRATEGY_CONFIG_PARSERS.
#   4. That's it — load_app_config() needs no changes.
#
# Each parser receives the full strategy YAML entry dict (the dict that also
# contains "name": "pmcc") and returns a typed config object.
# ===========================================================================

StrategyConfigParser = Callable[[Dict[str, Any]], Any]


def _parse_pmcc_config(raw: Dict[str, Any]) -> Optional[PmccConfig]:
    """Parse the 'pmcc:' block from a strategy entry."""
    p = raw.get("pmcc")
    if not p:
        return None
    r = p["risk"]
    return PmccConfig(
        underlying_symbol=p["underlying_symbol"],
        max_units=int(p["max_units"]),
        leap=PmccLeapConfig(**p["leap"]),
        short=PmccShortConfig(**p["short"]),
        roll=PmccRollConfig(**p["roll"]),
        liquidity=PmccLiquidityConfig(**p["liquidity"]),
        risk=PmccRiskConfig(
            max_buying_power_fraction=float(r["max_buying_power_fraction"]),
            max_debit_per_spread_usd=float(r["max_debit_per_spread_usd"]),
            max_contracts_per_intent=int(r["max_contracts_per_intent"]),
            slippage_factor=float(r["slippage_factor"]),
            ignore_spread_checks=bool(r.get("ignore_spread_checks", False)),
        ),
    )


# Registry: strategy name → parser function.
# Add new strategies here without touching load_app_config().
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

    raw = yaml.safe_load(path.read_text())

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

    return AppConfig(
        config_path=path,
        default_dry_run=bool(raw.get("default_dry_run", False)),
        cycle_seconds=float(raw.get("cycle_seconds", 60)),
        risk=global_risk,
        strategies=strategies,
    )