from __future__ import annotations

import sys
from pathlib import Path

from src.app.container import build_settings_runtime
from src.app.scheduler import Scheduler, SchedulerConfig
from src.brokers.factory import BrokerFactoryFacade
from src.brokers.registry import AlpacaBrokerBuilder, BrokerBuilderRegistry
from src.domain.intents import (
    CloseLegPayload,
    CloseSpreadPayload,
    EnterPmccPayload,
    RollNearPayload,
)
from src.execution.default_execution_policy import DefaultExecutionPolicy
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.orchestration.orchestrator import TradingOrchestrator
from src.persistence.interfaces import NullTradeDatabase
from src.persistence.trade_database import TradeDatabase
from src.risk.evaluators import PmccEvaluator, _EnterPmccApproved
from src.risk.pmcc_sizer import PmccSizer, PmccSizingConfig
from src.risk.risk_engine import RiskEngine
from src.risk.rules.open_order_rule import OpenOrderDedupeRule
from src.signals.signal_pipeline import IvRegimePipelineWithBars
from src.strategies.pmcc_strategy import PmccStrategy
from src.utilities.clock import LiveClock
from src.utilities.logger import setup_logger

logger = setup_logger("Main")

_DEFAULT_DB_PATH = Path("data/trades.db")

# ---------------------------------------------------------------------------
# Strategy registry — plain dict, no ABC, no plugin class.
# Each entry: { "build": Callable[[StrategySpec], StrategyABC],
#               "register": Callable[[StrategySpec, RiskEngine]] }
# Add a new strategy by adding one entry here.
# ---------------------------------------------------------------------------
def _pmcc_build(spec):
    return PmccStrategy(config=spec.pmcc)

def _pmcc_register(spec, engine: RiskEngine) -> None:
    r = spec.pmcc.risk
    sizing_cfg = PmccSizingConfig(
        max_buying_power_fraction=r.max_buying_power_fraction,
        max_debit_per_spread_usd=r.max_debit_per_spread_usd,
        max_contracts_per_intent=r.max_contracts_per_intent,
        slippage_factor=r.slippage_factor,
    )
    evaluator = PmccEvaluator(
        sizer=PmccSizer(config=sizing_cfg),
        max_units=spec.pmcc.max_units,
    )
    # Register the evaluator for all PMCC payload types.
    # _EnterPmccApproved is the internal sizing output — the evaluator
    # already wraps it, but we register the source type for dispatch.
    engine.register(EnterPmccPayload,    evaluator)
    engine.register(RollNearPayload,     evaluator)
    engine.register(CloseLegPayload,     evaluator)
    engine.register(CloseSpreadPayload,  evaluator)
    engine.register(_EnterPmccApproved,  evaluator)

STRATEGY_REGISTRY = {
    "pmcc": {"build": _pmcc_build, "register": _pmcc_register},
}


def main() -> int:
    settings = build_settings_runtime()
    app_cfg  = settings.app_cfg

    logger.info("Trading bot starting | config=%s", app_cfg.config_path)

    clock  = LiveClock()
    broker = _build_broker(settings.env_cfg)
    db     = _build_database()

    strategies  = _build_strategies(app_cfg.strategies)
    risk_engine = _build_risk_engine(app_cfg)

    orchestrator = TradingOrchestrator(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        execution_policy=DefaultExecutionPolicy(db=db, dry_run=app_cfg.default_dry_run),
        snapshot_builder=CycleSnapshotBuilder(broker=broker, clock=clock),
        signal_pipeline=IvRegimePipelineWithBars(underlying=app_cfg.strategies[0].pmcc.underlying_symbol, broker=broker),
        dry_run=app_cfg.default_dry_run,
        db=db,
    )

    scheduler = Scheduler(
        orchestrator=orchestrator,
        config=SchedulerConfig(cycle_seconds=app_cfg.cycle_seconds, order_check_seconds=app_cfg.order_check_seconds),
    )
    scheduler.run()
    return 0


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------

def _build_broker(env_cfg):
    registry = BrokerBuilderRegistry()
    registry.register("alpaca", AlpacaBrokerBuilder())
    return BrokerFactoryFacade(registry=registry).build_broker(env_cfg=env_cfg)


def _build_strategies(specs):
    strategies = []
    for spec in specs:
        entry = STRATEGY_REGISTRY.get(spec.name)
        if entry is None:
            logger.error("No strategy registered for name '%s'", spec.name)
            sys.exit(1)
        strategies.append(entry["build"](spec))
    return strategies


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


def _build_risk_engine(app_cfg) -> RiskEngine:
    engine = RiskEngine(
        rules=[OpenOrderDedupeRule()] if app_cfg.risk.enable_open_order_dedupe else []
    )
    for spec in app_cfg.strategies:
        entry = STRATEGY_REGISTRY.get(spec.name)
        if entry:
            entry["register"](spec, engine)
    return engine


if __name__ == "__main__":
    sys.exit(main())