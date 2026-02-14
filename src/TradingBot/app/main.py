from __future__ import annotations

import signal

from TradingBot.app.container import SettingsRuntime, build_settings_runtime
from TradingBot.app.scheduler import Scheduler, SchedulerConfig
from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.brokers.factory import BrokerFactoryFacade
from TradingBot.brokers.registry import AlpacaBrokerBuilder, BrokerBuilderRegistry
from TradingBot.execution.default_execution_policy import DefaultExecutionPolicy
from TradingBot.orchestration.orchestrator import TradingOrchestrator
from TradingBot.risk.pmcc_sizer import PmccSizer, PmccSizingConfig
from TradingBot.risk.risk_engine import RiskEngine
from TradingBot.risk.rules.open_order_rule import OpenOrderDedupeRule
from TradingBot.strategies.pmcc_strategy import PmccStrategy
from TradingBot.strategies.strategy_interface import StrategyABC
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("Main")


def _build_broker(settings: SettingsRuntime) -> BrokerABC:

    registry = BrokerBuilderRegistry()
    registry.register("alpaca", AlpacaBrokerBuilder())

    broker_factory = BrokerFactoryFacade(registry=registry)
    return broker_factory.build_broker(app_cfg=settings.env_cfg)


def _build_strategies(settings: SettingsRuntime) -> list[StrategyABC]:
    strategies: list[StrategyABC] = []
    for spec in settings.app_cfg.strategies:
        if spec.name == "pmcc":
            if spec.pmcc is None:
                raise ValueError("PMCC StrategySpec missing pmcc config")
            strategies.append(PmccStrategy(config=spec.pmcc))
            continue

        raise ValueError(f"Unsupported strategy name: {spec.name}")

    if not strategies:
        raise ValueError("No strategies were built. Check config/config.yaml -> strategies")
    return strategies


def _build_risk_engine(settings: SettingsRuntime) -> RiskEngine:
    pmcc_specs = [s for s in settings.app_cfg.strategies if s.name == "pmcc" and s.pmcc is not None]
    if not pmcc_specs:
        raise ValueError("RiskEngine requires a PMCC strategy config, but none was found.")

    pmcc_cfg = pmcc_specs[0].pmcc
    if pmcc_cfg is None:
        raise ValueError("PMCC StrategySpec missing pmcc config")

    sizer_cfg = PmccSizingConfig(
        equity_budget_pct=float(pmcc_cfg.risk.equity_budget_pct),
        max_option_bp_fraction=float(pmcc_cfg.risk.max_option_bp_fraction),
        max_debit_per_spread_usd=float(pmcc_cfg.risk.max_debit_per_spread_usd),
        max_contracts_per_intent=int(pmcc_cfg.risk.max_contracts_per_intent),
        slippage_factor=float(pmcc_cfg.risk.slippage_factor),
        max_leap_spread_pct=float(pmcc_cfg.liquidity.max_leap_spread_pct),
        max_near_spread_pct=float(pmcc_cfg.liquidity.max_near_spread_pct),
        ignore_spread_checks=bool(pmcc_cfg.risk.ignore_spread_checks),
    )

    pmcc_sizer = PmccSizer(sizer_cfg)

    rules = []
    if settings.app_cfg.risk.enable_open_order_dedupe:
        rules.append(OpenOrderDedupeRule())

    return RiskEngine(rules=rules, pmcc_sizer=pmcc_sizer)


def main() -> None:
    logger.warning("SIGINT handler at start of main: %r", signal.getsignal(signal.SIGINT))

    with log_scope("main", logger):
        settings = build_settings_runtime()

        logger.info(
            "Loaded settings | yaml=%s default_dry_run=%s cycle_seconds=%s broker=%s alpaca_mode=%s",
            str(settings.app_cfg.config_path),
            settings.app_cfg.default_dry_run,
            settings.app_cfg.cycle_seconds,
            settings.env_cfg.broker_name,
            settings.env_cfg.alpaca.mode,
        )

        broker = _build_broker(settings)
        strategies = _build_strategies(settings)
        risk_engine = _build_risk_engine(settings)

        execution_policy = DefaultExecutionPolicy()

        orchestrator = TradingOrchestrator(
            broker=broker,
            strategies=strategies,
            risk_engine=risk_engine,
            execution_policy=execution_policy,
            dry_run=settings.app_cfg.default_dry_run,
        )

        scheduler = Scheduler(
            orchestrator=orchestrator,
            config=SchedulerConfig(cycle_seconds=settings.app_cfg.cycle_seconds),
        )

        logger.info("Built bot | strategies=%d dry_run=%s", len(strategies), settings.app_cfg.default_dry_run)

        scheduler.run_forever()


if __name__ == "__main__":
    main()


'''
Now, design patterns used in the codebase (where they live and what they do):

Composition root

Where: src/TradingBot/app/main.py

Purpose: The one place that assembles the application graph (broker, strategies, risk engine, execution policy, orchestrator, scheduler). Keeps wiring out of domain logic.

Dependency injection

Where: Everywhere assembly happens from main.py into constructors:

TradingOrchestrator(broker, strategies, risk_engine, execution_policy, dry_run)

RiskEngine(rules, pmcc_sizer)

PmccStrategy(config=...)

Purpose: Classes do not fetch global config; they receive dependencies explicitly.

Factory + Registry

Where:

Factory facade: src/TradingBot/brokers/factory.py (BrokerFactoryFacade)

Registry: src/TradingBot/brokers/registry.py (BrokerBuilderRegistry, AlpacaBrokerBuilder)

Pattern: Factory constructs a broker; Registry maps broker name -> builder.

Purpose: Add brokers without changing orchestrator/strategy logic. Open/Closed Principle.

Adapter

Where: src/TradingBot/brokers/alpaca_broker.py (and broker interface module)

Pattern: Broker implementation adapts Alpaca SDK into your internal BrokerABC interface.

Purpose: Rest of system depends on your abstractions, not Alpaca types.

Strategy pattern

Where: src/TradingBot/strategies/strategy_interface.py and pmcc_strategy.py

Pattern: Different trading strategies implement the same interface (symbols(), generate_intents() etc).

Purpose: Orchestrator runs a list of strategies uniformly.

Rule engine (Chain of responsibility style)

Where: src/TradingBot/risk/risk_engine.py + src/TradingBot/risk/rules/* (e.g., open_order_rule.py)

Pattern: Risk engine applies a sequence of rules to intents.

Purpose: Each rule is isolated (single responsibility), easy to add/remove.

Policy pattern

Where: src/TradingBot/execution/default_execution_policy.py

Pattern: Execution policy converts approved intents into concrete broker orders.

Purpose: Swap execution behaviour (single-leg, multi-leg, market vs limit) without touching strategies/risk.

Builder pattern (snapshot builder)

Where: src/TradingBot/orchestration/snapshot_builder.py (or similar; the component that builds CycleSnapshot)

Pattern: Builds an aggregate “cycle snapshot” from multiple broker calls.

Purpose: Consolidate IO, keep orchestrator and strategies working with one coherent context object.

Template method / Orchestrator loop

Where: src/TradingBot/orchestration/orchestrator.py (run_cycle calling _build_snapshot, _collect_intents, _evaluate, _to_orders, _submit)

Pattern: Fixed high-level algorithm with internal steps.

Purpose: Guarantees cycle structure; each step can evolve independently.

State machine (this is the missing “done properly” part)

Where: Intended primarily inside pmcc_strategy.py (and/or a dedicated pmcc_state_machine.py)

Pattern: State machine governing the lifecycle of the PMCC position:

No position -> open both legs

Only LEAP held -> sell NEAR

Both legs held -> monitor -> roll/close NEAR

Partial fills / pending orders -> reconcile

Purpose: Make cycle-to-cycle behaviour deterministic. This is exactly what your “management flag” relates to.
'''