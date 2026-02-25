from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from src.app.container import build_settings_runtime
from src.backtest.alpaca_historical_broker import AlpacaHistoricalBroker
from src.backtest.backtest_runner import BacktestRunner
from src.backtest.simulated_account import SimulatedAccount
from src.config.yaml_config import AppConfig, BacktestConfig, StrategySpec
from src.execution.default_execution_policy import DefaultExecutionPolicy
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.orchestration.orchestrator import TradingOrchestrator
from src.persistence.interfaces import NullTradeDatabase
from src.persistence.trade_database import TradeDatabase
from src.risk.risk_engine import RiskEngine
from src.risk.rules.open_order_rule import OpenOrderDedupeRule
from src.signals.signal_pipeline import DefaultSignalPipeline
from src.strategies.interfaces import StrategyABC
from src.utilities.clock import BacktestClock
from src.utilities.logger import setup_logger

logger = setup_logger("RunBacktest")

_NOISY_LOGGERS = [
    "CycleSnapshotBuilder", "AlpacaHistoricalBroker",
    "PmccContractSelector", "PmccStateClassifier", "PmccStrategy",
    "RiskEngine", "TradingOrchestrator", "BrokerBase",
]


def _configure_logging(log_mode: str) -> None:
    if log_mode == "progress":
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.ERROR)


def _build_broker(bt_cfg: BacktestConfig, clock: BacktestClock) -> AlpacaHistoricalBroker:
    api_key    = os.getenv("ALPACA_PAPER_API_KEY", "").strip()
    api_secret = os.getenv("ALPACA_PAPER_API_SECRET", "").strip()
    if not api_key or not api_secret:
        logger.error(
            "Missing Alpaca credentials. "
            "Set ALPACA_PAPER_API_KEY and ALPACA_PAPER_API_SECRET."
        )
        sys.exit(1)
    account = SimulatedAccount(initial_cash=bt_cfg.initial_cash)
    return AlpacaHistoricalBroker(
        account=account,
        api_key=api_key,
        api_secret=api_secret,
        clock=clock,
    )


def _build_strategies(specs: list[StrategySpec]) -> list[StrategyABC]:
    # Import here to avoid circular deps at module level
    from src.app.main import STRATEGY_REGISTRY
    strategies = []
    for spec in specs:
        entry = STRATEGY_REGISTRY.get(spec.name)
        if entry is None:
            logger.error("No strategy registered for '%s'", spec.name)
            sys.exit(1)
        strategies.append(entry["build"](spec))
    return strategies


def _build_risk_engine(app_cfg: AppConfig) -> RiskEngine:
    from src.app.main import STRATEGY_REGISTRY
    engine = RiskEngine(
        rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else []
    )
    for spec in app_cfg.strategies:
        entry = STRATEGY_REGISTRY.get(spec.name)
        if entry:
            entry["register"](spec, engine)
    return engine


def _build_database(bt_cfg: BacktestConfig):
    db_path = bt_cfg.output_dir / "trades.db"
    try:
        return TradeDatabase(db_path=db_path)
    except Exception as exc:
        logger.warning("Could not initialise TradeDatabase (%s) — using NullTradeDatabase", exc)
        return NullTradeDatabase()


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg
    bt_cfg   = app_cfg.backtest
    if bt_cfg is None:
        logger.error("No [backtest] section found in config.yaml.")
        return 1

    _configure_logging(bt_cfg.log_mode)

    logger.info(
        "Backtest config | start=%s end=%s capital=$%.0f output=%s mode=%s",
        bt_cfg.start_date, bt_cfg.end_date,
        bt_cfg.initial_cash, bt_cfg.output_dir, bt_cfg.log_mode,
    )

    clock  = BacktestClock()
    broker = _build_broker(bt_cfg, clock)
    db     = _build_database(bt_cfg)

    strategies  = _build_strategies(app_cfg.strategies)
    risk_engine = _build_risk_engine(app_cfg)

    orchestrator = TradingOrchestrator(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        execution_policy=DefaultExecutionPolicy(),
        snapshot_builder=CycleSnapshotBuilder(broker=broker, clock=clock),
        signal_pipeline=DefaultSignalPipeline(),
        dry_run=False,
    )

    runner = BacktestRunner(
        orchestrator=orchestrator,
        broker=broker,
        clock=clock,
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
        logger.warning("Chart generation skipped: %s", exc)

    fills = db.get_fills()
    if fills:
        print(f"\n  {len(fills)} fills persisted → {bt_cfg.output_dir / 'trades.db'}")
    else:
        print("\n  No fills recorded (NullTradeDatabase or zero trades).")

    return 0


if __name__ == "__main__":
    sys.exit(main())