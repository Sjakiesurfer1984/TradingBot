from __future__ import annotations

import sys

from src.app.container import build_settings_runtime
from src.app.scheduler import Scheduler, SchedulerConfig
from src.brokers.factory import BrokerFactoryFacade
from src.brokers.registry import AlpacaBrokerBuilder, BrokerBuilderRegistry
from src.execution.default_execution_policy import DefaultExecutionPolicy
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.orchestration.orchestrator import TradingOrchestrator
from src.risk.evaluators import OptionIntentEvaluator, PmccIntentEvaluator
from src.risk.pmcc_sizer import PmccSizer, PmccSizingConfig
from src.risk.risk_engine import RiskEngine
from src.risk.rules.open_order_rule import OpenOrderDedupeRule
from src.signals.signal_pipeline import DefaultSignalPipeline
from src.strategies.pmcc_strategy import PmccStrategy
from src.strategies.registry import StrategyBuilderRegistry
from src.utilities.logger import setup_logger
from src.domain.intents import PmccIntentPayload, OptionIntentPayload

logger = setup_logger("Main")


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg

    logger.info("Trading bot starting | config=%s", app_cfg.config_path)

    broker  = _build_broker(settings.env_cfg)
    strategies = _build_strategies(app_cfg.strategies)
    risk_engine = _build_risk_engine(app_cfg)

    orchestrator = TradingOrchestrator(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        execution_policy=DefaultExecutionPolicy(),
        snapshot_builder=CycleSnapshotBuilder(broker=broker),
        signal_pipeline=DefaultSignalPipeline(),
        dry_run=app_cfg.default_dry_run,
    )

    scheduler = Scheduler(
        orchestrator=orchestrator,
        config=SchedulerConfig(cycle_seconds=app_cfg.cycle_seconds),
    )
    scheduler.run()
    return 0


def _build_broker(env_cfg):
    registry = BrokerBuilderRegistry()
    registry.register("alpaca", AlpacaBrokerBuilder())
    return BrokerFactoryFacade(registry=registry).build_broker(env_cfg=env_cfg)


def _build_strategies(specs):
    registry = StrategyBuilderRegistry()
    registry.register("pmcc", lambda spec: PmccStrategy(config=spec.pmcc))
    return registry.build_all(specs)


def _build_risk_engine(app_cfg):
    engine = RiskEngine(rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else [])

    # Find PMCC config for sizer — use first PMCC strategy found
    pmcc_risk_cfg = next(
        (s.pmcc.risk for s in app_cfg.strategies if s.pmcc is not None),
        None,
    )

    sizing_cfg = PmccSizingConfig(
        equity_budget_pct=pmcc_risk_cfg.equity_budget_pct            if pmcc_risk_cfg else 0.05,
        max_option_bp_fraction=pmcc_risk_cfg.max_option_bp_fraction  if pmcc_risk_cfg else 0.25,
        max_debit_per_spread_usd=pmcc_risk_cfg.max_debit_per_spread_usd if pmcc_risk_cfg else 5000.0,
        max_contracts_per_intent=pmcc_risk_cfg.max_contracts_per_intent if pmcc_risk_cfg else 5,
        slippage_factor=pmcc_risk_cfg.slippage_factor                if pmcc_risk_cfg else 1.02,
    )
    max_units = next(
        (s.pmcc.max_units for s in app_cfg.strategies if s.pmcc is not None),
        2,
    )

    engine.register(PmccIntentPayload, PmccIntentEvaluator(sizer=PmccSizer(config=sizing_cfg), max_units=max_units))
    engine.register(OptionIntentPayload, OptionIntentEvaluator())
    return engine


if __name__ == "__main__":
    sys.exit(main())
