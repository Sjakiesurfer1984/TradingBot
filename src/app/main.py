from __future__ import annotations

import sys
from pathlib import Path

from src.app.container import build_settings_runtime
from src.app.market_calendar import AlwaysOpenMarketCalendar
from src.app.scheduler import Scheduler, SchedulerConfig
from src.brokers.factory import BrokerFactoryFacade
from src.brokers.interfaces import MarketCalendarProviderABC
from src.brokers.registry import AlpacaBrokerBuilder, BrokerBuilderRegistry
from src.config.yaml_config import StrategySpec
from src.domain.market_calendar import MarketCalendarABC
from src.execution.default_execution_policy import DefaultExecutionPolicy
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.orchestration.orchestrator import TradingOrchestrator
from src.persistence.interfaces import NullTradeDatabase
from src.persistence.reconciler import LivePositionReconciler
from src.persistence.trade_database import TradeDatabase
from src.risk.risk_engine import RiskEngine
from src.risk.rules.open_order_rule import OpenOrderDedupeRule
from src.strategies.strategy_plugin import STRATEGY_PLUGIN_REGISTRY
from src.utilities.clock import LiveClock
from src.utilities.logger import setup_logger

logger = setup_logger("Main")

_DEFAULT_DB_PATH = Path("data/trades.db")


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg

    logger.info("Trading bot starting | config=%s", app_cfg.config_path)

    clock  = LiveClock()
    broker = _build_broker(settings.env_cfg)
    db     = _build_database()

    spec        = app_cfg.strategies[0]
    plugin      = _get_plugin(spec)
    calendar    = _build_calendar(spec, broker)
    strategies  = _build_strategies(app_cfg.strategies)
    risk_engine = _build_risk_engine(app_cfg)

    orchestrator = TradingOrchestrator(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        execution_policy=DefaultExecutionPolicy(db=db, dry_run=app_cfg.default_dry_run),
        snapshot_builder=CycleSnapshotBuilder(broker=broker, clock=clock),
        signal_pipeline=plugin.build_signal_pipeline(spec, broker),
        dry_run=app_cfg.default_dry_run,
        db=db,
        reconciler=LivePositionReconciler(db=db),
    )

    scheduler = Scheduler(
        orchestrator=orchestrator,
        config=SchedulerConfig(
            cycle_seconds=app_cfg.cycle_seconds,
            order_check_seconds=app_cfg.order_check_seconds,
        ),
        calendar=calendar,
    )
    scheduler.run()
    return 0


# ---------------------------------------------------------------------------
# Wiring helpers — infrastructure only, zero strategy knowledge
# ---------------------------------------------------------------------------

def _get_plugin(spec: StrategySpec):
    plugin = STRATEGY_PLUGIN_REGISTRY.get(spec.name)
    if plugin is None:
        logger.error(
            "No strategy plugin registered for '%s'. "
            "Add it to STRATEGY_PLUGIN_REGISTRY in strategy_plugin.py.",
            spec.name,
        )
        sys.exit(1)
    return plugin


def _build_broker(env_cfg):
    registry = BrokerBuilderRegistry()
    registry.register("alpaca", AlpacaBrokerBuilder())
    return BrokerFactoryFacade(registry=registry).build_broker(env_cfg=env_cfg)


def _build_database():
    import os
    db_path = Path(os.getenv("TRADE_DB_PATH", str(_DEFAULT_DB_PATH)))
    try:
        return TradeDatabase(db_path=db_path)
    except Exception as exc:
        logger.warning(
            "Could not initialise TradeDatabase at %s (%s) — "
            "trades will NOT be persisted this session.",
            db_path, exc,
        )
        return NullTradeDatabase()


def _build_strategies(specs) -> list:
    strategies = []
    for spec in specs:
        strategies.append(_get_plugin(spec).build(spec))
    return strategies


def _build_risk_engine(app_cfg) -> RiskEngine:
    engine = RiskEngine(
        rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else []
    )
    for spec in app_cfg.strategies:
        _get_plugin(spec).register_evaluators(spec, engine)
    return engine


def _build_calendar(spec: StrategySpec, broker) -> MarketCalendarABC:
    """
    GoF Factory Method — selects the correct calendar from asset_class.
    asset_class is declared explicitly in config.yaml — no symbol sniffing.
    """
    if spec.asset_class == "crypto":
        logger.info(
            "Calendar | asset_class=crypto underlying=%s → AlwaysOpenMarketCalendar",
            spec.underlying_symbol,
        )
        return AlwaysOpenMarketCalendar()

    if spec.asset_class == "equity":
        if isinstance(broker, MarketCalendarProviderABC):
            logger.info(
                "Calendar | asset_class=equity underlying=%s → AlpacaMarketCalendar",
                spec.underlying_symbol,
            )
            return broker.get_market_calendar()
        logger.warning(
            "Calendar | broker does not implement MarketCalendarProviderABC "
            "— falling back to AlwaysOpenMarketCalendar"
        )
        return AlwaysOpenMarketCalendar()

    raise ValueError(
        f"No calendar implementation for asset_class='{spec.asset_class}'. "
        f"Add a branch to _build_calendar() in main.py."
    )


if __name__ == "__main__":
    sys.exit(main())