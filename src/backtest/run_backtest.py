from __future__ import annotations

import os
import sys
from pathlib import Path

from src.app.container import build_settings_runtime
from src.backtest.alpaca_historical_broker import AlpacaHistoricalBroker
from src.backtest.backtest_runner import BacktestRunner
from src.backtest.backtest_snapshot_builder import BacktestSnapshotBuilder
from src.backtest.simulated_account import SimulatedAccount
from src.execution.default_execution_policy import DefaultExecutionPolicy
from src.orchestration.orchestrator import TradingOrchestrator
from src.risk.risk_engine import RiskEngine
from src.risk.rules.open_order_rule import OpenOrderDedupeRule
from src.signals.signal_pipeline import DefaultSignalPipeline
from src.strategies.strategy_plugin import STRATEGY_PLUGINS
from src.utilities.logger import setup_logger

"""
Entry point for backtests.

Run from repo root:
    python -m src.backtest.run_backtest

Required env vars:
    ALPACA_PAPER_API_KEY
    ALPACA_PAPER_API_SECRET

All backtest parameters come from config.yaml [backtest] section.
Env vars override yaml:
    BACKTEST_START_DATE  BACKTEST_END_DATE
    BACKTEST_INITIAL_CASH  BACKTEST_OUTPUT_DIR  BACKTEST_LOG_MODE
"""

logger = setup_logger("RunBacktest")


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg
    bt_cfg   = app_cfg.backtest

    if bt_cfg is None:
        logger.error("No [backtest] section in config.yaml")
        return 1

    api_key    = os.getenv("ALPACA_PAPER_API_KEY", "").strip()
    api_secret = os.getenv("ALPACA_PAPER_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print(
            "\n[ERROR] Missing Alpaca credentials.\n"
            "  Set ALPACA_PAPER_API_KEY and ALPACA_PAPER_API_SECRET\n"
            "  in your .env file or environment before running a backtest.\n",
            file=sys.stderr,
        )
        return 1

    logger.info(
        "Backtest config | start=%s end=%s capital=$%.0f output=%s mode=%s",
        bt_cfg.start_date, bt_cfg.end_date,
        bt_cfg.initial_cash, bt_cfg.output_dir, bt_cfg.log_mode,
    )

    broker = AlpacaHistoricalBroker(
        account=SimulatedAccount(initial_cash=bt_cfg.initial_cash),
        api_key=api_key,
        api_secret=api_secret,
    )

    strategies = [
        STRATEGY_PLUGINS[spec.name].build_strategy(spec)
        for spec in app_cfg.strategies
        if spec.name in STRATEGY_PLUGINS
    ]

    risk_engine = RiskEngine(
        rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else []
    )
    for spec in app_cfg.strategies:
        plugin = STRATEGY_PLUGINS.get(spec.name)
        if plugin:
            plugin.register_evaluators(spec, risk_engine)

    orchestrator = TradingOrchestrator(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        execution_policy=DefaultExecutionPolicy(),
        snapshot_builder=BacktestSnapshotBuilder(broker=broker),  # ← key fix
        signal_pipeline=DefaultSignalPipeline(),
        dry_run=False,
    )

    runner = BacktestRunner(
        orchestrator=orchestrator,
        broker=broker,
        start_date=bt_cfg.start_date,
        end_date=bt_cfg.end_date,
        output_dir=bt_cfg.output_dir,
        log_mode=bt_cfg.log_mode,
    )

    results = runner.run()

    try:
        from src.backtest.plot_results import plot_backtest
        chart_path = plot_backtest(
            day_results=results,
            trade_log=broker.account.trade_log(),
            output_dir=bt_cfg.output_dir,
        )
        print(f"\n  Chart saved → {chart_path}")
    except Exception as exc:
        logger.warning("Chart generation failed: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())