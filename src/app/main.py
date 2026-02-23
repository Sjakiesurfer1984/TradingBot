"""
src/app/main.py
----------------
Application entry point. Pure assembler — no strategy-specific logic lives here.

To add a new strategy:
  1. Implement StrategyPlugin in src/strategies/strategy_plugin.py
  2. Add its config parser to src/config/yaml_config._STRATEGY_CONFIG_PARSERS
  3. That's it. This file does not change.
"""
from __future__ import annotations

import sys

from src.app.container import build_settings_runtime
from src.app.scheduler import Scheduler, SchedulerConfig
from src.brokers.factory import BrokerFactoryFacade
from src.brokers.registry import AlpacaBrokerBuilder, BrokerBuilderRegistry
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

logger = setup_logger("Main")


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg

    logger.info("Trading bot starting | config=%s", app_cfg.config_path)

    broker      = _build_broker(settings.env_cfg)
    strategies  = _build_strategies(app_cfg.strategies)
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
    registry = BrokerBuilderRegistry() # The registry is where we register all broker builders, and the factory facade uses it to construct brokers from config.
    registry.register("alpaca", AlpacaBrokerBuilder())
    return BrokerFactoryFacade(registry=registry).build_broker(env_cfg=env_cfg)


def _build_strategies(specs: list[StrategySpec]) -> list[StrategyABC]:
    """
    Build all strategy instances via the plugin registry.
    Each plugin knows how to construct its own strategy from the spec.
    No strategy-specific logic here.
    """
    strategies = []
    for spec in specs:
        plugin = STRATEGY_PLUGINS.get(spec.name)
        if plugin is None:
            logger.error(
                "No plugin registered for strategy '%s'. "
                "Registered plugins: %s. "
                "Add a StrategyPlugin subclass to strategy_plugin.py.",
                spec.name, list(STRATEGY_PLUGINS),
            )
            sys.exit(1)
        strategies.append(plugin.build_strategy(spec))
    return strategies


def _build_risk_engine(app_cfg: AppConfig) -> RiskEngine:
    """
    Build the risk engine and let each plugin register its own evaluators.
    No strategy-specific logic here — evaluator wiring belongs to the plugin.
    """
    engine = RiskEngine(
        rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else []
    )
    for spec in app_cfg.strategies:
        plugin = STRATEGY_PLUGINS.get(spec.name)
        if plugin is None:
            logger.error(
                "No plugin registered for strategy '%s'. "
                "Registered plugins: %s.",
                spec.name, list(STRATEGY_PLUGINS),
            )
            sys.exit(1)
        plugin.register_evaluators(spec, engine)
    return engine


if __name__ == "__main__":
    sys.exit(main())