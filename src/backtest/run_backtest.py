"""
src/backtest/run_backtest.py
-----------------------------
Entry point for running a backtest. Wire everything together here —
the same pattern as main.py but using BacktestRunner instead of Scheduler
and AlpacaHistoricalBroker instead of AlpacaBroker.

Run from the repo root:
    python -m src.backtest.run_backtest

Environment variables required (same as live):
    ALPACA_PAPER_API_KEY
    ALPACA_PAPER_API_SECRET

Optional overrides via env vars:
    BACKTEST_START_DATE   e.g. 2023-01-01  (default: 1 year ago)
    BACKTEST_END_DATE     e.g. 2024-01-01  (default: today)
    BACKTEST_INITIAL_CASH e.g. 100000      (default: 100000)
    BACKTEST_OUTPUT_DIR   e.g. ./results   (default: ./backtest_results)
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

from src.app.container import build_settings_runtime
from src.backtest.alpaca_historical_broker import AlpacaHistoricalBroker
from src.backtest.backtest_runner import BacktestRunner
from src.backtest.simulated_account import SimulatedAccount
from src.config.yaml_config import AppConfig, StrategySpec
from src.execution.default_execution_policy import DefaultExecutionPolicy
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.orchestration.orchestrator import TradingOrchestrator
from src.risk.risk_engine import RiskEngine
from src.risk.rules.open_order_rule import OpenOrderDedupeRule
from src.signals.signal_pipeline import DefaultSignalPipeline
from src.strategies.interfaces import StrategyABC
from src.strategies.strategy_plugin import STRATEGY_PLUGINS
from src.utilities.logger import setup_logger

logger = setup_logger("RunBacktest")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _build_broker(app_cfg: AppConfig) -> AlpacaHistoricalBroker:
    api_key    = _env("ALPACA_PAPER_API_KEY")
    api_secret = _env("ALPACA_PAPER_API_SECRET")
    if not api_key or not api_secret:
        logger.error(
            "Missing Alpaca credentials. Set ALPACA_PAPER_API_KEY and "
            "ALPACA_PAPER_API_SECRET in your .env or environment."
        )
        sys.exit(1)

    initial_cash = float(_env("BACKTEST_INITIAL_CASH", "100000"))
    account      = SimulatedAccount(initial_cash=initial_cash)

    return AlpacaHistoricalBroker(
        account=account,
        api_key=api_key,
        api_secret=api_secret,
    )


def _build_strategies(specs: list[StrategySpec]) -> list[StrategyABC]:
    strategies = []
    for spec in specs:
        plugin = STRATEGY_PLUGINS.get(spec.name)
        if plugin is None:
            logger.error("No plugin for strategy '%s'", spec.name)
            sys.exit(1)
        strategies.append(plugin.build_strategy(spec))
    return strategies


def _build_risk_engine(app_cfg: AppConfig, broker) -> RiskEngine:
    engine = RiskEngine(
        rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else []
    )
    for spec in app_cfg.strategies:
        plugin = STRATEGY_PLUGINS.get(spec.name)
        if plugin:
            plugin.register_evaluators(spec, engine)
    return engine


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg

    # ------------------------------------------------------------------
    # Date range
    # ------------------------------------------------------------------
    start_str = _env("BACKTEST_START_DATE")
    end_str   = _env("BACKTEST_END_DATE")

    start_date = (
        date.fromisoformat(start_str)
        if start_str else
        date.today() - timedelta(days=365)
    )
    end_date = (
        date.fromisoformat(end_str)
        if end_str else
        date.today()
    )

    if start_date >= end_date:
        logger.error("BACKTEST_START_DATE must be before BACKTEST_END_DATE")
        return 1

    output_dir = Path(_env("BACKTEST_OUTPUT_DIR", "backtest_results"))

    logger.info(
        "Backtest config | start=%s end=%s output=%s",
        start_date, end_date, output_dir,
    )

    # ------------------------------------------------------------------
    # Wire everything together
    # ------------------------------------------------------------------
    broker      = _build_broker(app_cfg)
    strategies  = _build_strategies(app_cfg.strategies)
    risk_engine = _build_risk_engine(app_cfg, broker)

    orchestrator = TradingOrchestrator(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        execution_policy=DefaultExecutionPolicy(),
        snapshot_builder=CycleSnapshotBuilder(broker=broker),
        signal_pipeline=DefaultSignalPipeline(),
        dry_run=False,   # backtest always executes fills
    )

    runner = BacktestRunner(
        orchestrator=orchestrator,
        broker=broker,
        start_date=start_date,
        end_date=end_date,
        output_dir=output_dir,
    )

    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
