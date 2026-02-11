# src/TradingBot/v2/main.py
from __future__ import annotations

import os
import signal
import time
from threading import Event
from typing import Optional

from dotenv import load_dotenv

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.brokers.factory import BrokerFactoryFacade
from TradingBot.brokers.registry import BrokerBuilderRegistry, AlpacaBrokerBuilder

from TradingBot.config.runtime_config import RuntimeConfig, load_runtime_config
from TradingBot.config.settings import load_app_config_from_env

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

from TradingBot.orchestration.orchestrator import TradingOrchestrator
from TradingBot.risk.pmcc_sizer import PmccSizer, PmccSizingConfig
from TradingBot.risk.risk_engine import RiskEngine
from TradingBot.risk.rules.open_order_rule import OpenOrderDedupeRule

from TradingBot.strategies.pmcc_strategy import PmccStrategy
from TradingBot.strategies.strategy_interface import StrategyABC

from TradingBot.execution.default_execution_policy import DefaultExecutionPolicy

from TradingBot.app.scheduler import SchedulerConfig, Scheduler


logger = setup_logger("Main")

# The _STOP_REQUESTED event is set when a SIGINT is received, and serves as a signal to gracefully stop operations.
# _LAST_SIGINT_TS records the timestamp of the last SIGINT to handle double interrupts.
# SIGINT is a keyboard interrupt (Ctrl+C).
#
# Note: This file does not yet use _STOP_REQUESTED inside Scheduler.
# It is kept here because it is the foundation for a clean shutdown path:
# - First Ctrl+C: request a graceful stop
# - Second Ctrl+C within a short window: exit immediately
_STOP_REQUESTED = Event()
_LAST_SIGINT_TS: float = 0.0


def _get_env(name: str) -> Optional[str]:
    """
    Read an environment variable safely.

    Return value
    - The stripped string value if present and non-empty
    - None if missing or empty

    Why this exists
    - Environment variables are strings and can be missing.
    - Centralising the parsing avoids repeated, inconsistent checks.
    """
    value: Optional[str] = os.getenv(name)
    if value is None:
        return None
    stripped: str = value.strip()
    return stripped if stripped else None


def _get_env_bool(name: str, default: bool) -> bool:
    """
    Read an environment variable as a boolean.

    Accepted truthy values
    - 1, true, yes, y (case-insensitive)

    Everything else is treated as False when the variable is present.
    If missing, returns the provided default.

    Why this exists
    - The program must behave predictably when variables are missing.
    - It must also be clear which values count as 'True'.
    """
    raw: Optional[str] = _get_env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y"}


def _mask_secret(value: Optional[str], show_prefix: int = 4) -> str:
    """
    Mask secrets in logs.

    Why this exists
    - Logs must never leak API keys or secrets.
    - When debugging, it is still helpful to confirm which key is loaded.
      Showing a short prefix provides that confidence without exposing the key.
    """
    if value is None:
        return "<missing>"
    v: str = value.strip()
    if not v:
        return "<missing>"
    if len(v) <= show_prefix:
        return "*" * len(v)
    return f"{v[:show_prefix]}{'*' * (len(v) - show_prefix)}"


def _log_env_snapshot() -> None:
    """
    Log a safe snapshot of configuration and critical env vars.

    Why this exists
    - When the bot does something unexpected, the first question is often:
      'What configuration did it actually start with?'
    - This log is the answer, but it must never print secrets.
    """
    broker_name: str = os.getenv("TRADINGBOT_BROKER", "fake")

    dry_run_raw: Optional[str] = _get_env("TBOT_DRY_RUN")
    alpaca_mode: Optional[str] = _get_env("ALPACA_MODE")

    paper_key: Optional[str] = _get_env("ALPACA_PAPER_API_KEY")
    paper_secret: Optional[str] = _get_env("ALPACA_PAPER_API_SECRET")
    live_key: Optional[str] = _get_env("ALPACA_LIVE_API_KEY")
    live_secret: Optional[str] = _get_env("ALPACA_LIVE_API_SECRET")

    logger.info("Config snapshot begin")
    logger.info("TRADINGBOT_BROKER=%s", broker_name)
    logger.info("TBOT_DRY_RUN=%s", dry_run_raw if dry_run_raw is not None else "<default>")
    logger.info("ALPACA_MODE=%s", alpaca_mode if alpaca_mode is not None else "<default paper>")
    logger.info("ALPACA_PAPER_API_KEY=%s", _mask_secret(paper_key))
    logger.info("ALPACA_PAPER_API_SECRET=%s", _mask_secret(paper_secret))
    logger.info("ALPACA_LIVE_API_KEY=%s", _mask_secret(live_key))
    logger.info("ALPACA_LIVE_API_SECRET=%s", _mask_secret(live_secret))
    logger.info("Config snapshot end")


def _install_interrupt_tracer() -> None:
    """
    Install a signal handler for Ctrl+C (SIGINT).

    Why this exists
    - A trading bot should stop safely.
    - The first interrupt requests a graceful stop.
    - A second interrupt shortly afterwards forces an immediate exit.

    Current limitation
    - Scheduler does not yet consult _STOP_REQUESTED, so this handler mainly
      serves as an audit trail and a foundation for a future stop mechanism.
    """

    def _handler(signum: int, frame) -> None:
        global _LAST_SIGINT_TS
        now: float = time.time()

        logger.error("Interrupt received | signum=%s time=%s", signum, now)

        if _STOP_REQUESTED.is_set() and (now - _LAST_SIGINT_TS) < 2.0:
            logger.critical("Second interrupt received quickly; exiting immediately.")
            raise KeyboardInterrupt

        _LAST_SIGINT_TS = now
        _STOP_REQUESTED.set()
        logger.warning("Stop requested. Press Ctrl+C again to force quit.")

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handler)


def _build_risk_engine() -> RiskEngine:
    """
    Construct the risk engine and its dependencies.

    Design patterns used here

    Strategy pattern (rules list)
    - Each RiskRule evaluates an intent independently.
    - The RiskEngine applies the list of rules in order.

    Composition over inheritance
    - RiskEngine is configured by injecting rule objects and a sizer object.
    - This makes behaviour easy to change without editing RiskEngine itself.

    Why this exists
    - Risk checks and sizing can grow complex.
    - Building them in one place keeps main() readable and keeps wiring explicit.
    """
    with log_scope("build_risk_engine", logger):
        # Each rule is responsible for exactly one concern.
        # OpenOrderDedupeRule prevents duplicate orders.
        cfg = PmccSizingConfig(
            # These parameters define the safety envelope for PMCC sizing.
            # They are read from env so you can adjust risk without changing code.
            equity_budget_pct=float(os.getenv("TBOT_PMCC_EQUITY_BUDGET_PCT", "0")),
            max_option_bp_fraction=float(os.getenv("TBOT_PMCC_MAX_OPTION_BP_FRACTION", "0")),
            max_debit_per_spread_usd=float(os.getenv("TBOT_PMCC_MAX_DEBIT_PER_SPREAD_USD", "0")),
            max_contracts_per_intent=int(os.getenv("TBOT_PMCC_MAX_CONTRACTS_PER_INTENT", "0")),
            slippage_factor=float(os.getenv("TBOT_PMCC_SLIPPAGE_FACTOR", "1.0")),
            max_leap_spread_pct=float(os.getenv("TBOT_PMCC_MAX_LEAP_SPREAD_PCT", "0")),
            max_near_spread_pct=float(os.getenv("TBOT_PMCC_MAX_NEAR_SPREAD_PCT", "0")),
            ignore_spread_checks=_get_env_bool("TBOT_PMCC_IGNORE_SPREAD_CHECKS", default=False),
        )

        # PmccSizer is responsible for turning 'available capital + constraints'
        # into a quantity and limit price suggestion.
        pmcc_sizer: PmccSizer = PmccSizer(cfg)

        # RiskEngine applies rule objects and then sizes PMCC intents.
        # It produces policy-neutral approvals that the execution policy converts into orders.
        risk_engine = RiskEngine(
            rules=[OpenOrderDedupeRule()],
            pmcc_sizer=pmcc_sizer,
        )
        return risk_engine


def _build_strategies(runtime_cfg: RuntimeConfig) -> list[StrategyABC]:
    """
    Build the strategy objects declared in the runtime configuration.

    Design pattern used here

    Factory (simple selection factory)
    - This function selects which concrete Strategy class to instantiate
      based on runtime configuration.
    - Each strategy object is created with only its own required config.

    Why this exists
    - Strategies are the primary extension point for the trading system.
    - Adding a new strategy should require adding one new concrete class
      and one new wiring branch here, without changing unrelated components.
    """
    strategies: list[StrategyABC] = []

    for spec in runtime_cfg.strategies:
        if spec.name == "pmcc":
            if spec.pmcc is None:
                raise ValueError("PMCC StrategySpec missing pmcc config")

            cfg = spec.pmcc

            # PmccStrategy will declare its universe() so the orchestrator can
            # fetch the correct snapshot data for each cycle.
            strategies.append(PmccStrategy(underlying_symbol=cfg.underlying_symbol))
            continue

        raise ValueError(f"Unsupported strategy name: {spec.name}")

    return strategies


def main() -> None:
    """
    Entry point.

    High-level flow
    - Load environment variables from .env.
    - Load runtime config (which strategies to run, etc).
    - Load app config (broker settings and secrets).
    - Build broker via broker factory facade and registry.
    - Build risk engine, strategies, execution policy.
    - Build orchestrator by injecting dependencies (composition).
    - Run the scheduler forever.

    Design patterns used in this file

    Facade
    - BrokerFactoryFacade provides a single method to construct the broker.
    - This keeps broker wiring out of main().

    Registry
    - BrokerBuilderRegistry maps broker name -> builder object.
    - Adding a broker means registering a new builder, without changing the factory.

    Factory (builders)
    - AlpacaBrokerBuilder constructs the concrete Alpaca broker instance.

    Strategy
    - Strategies implement StrategyABC and provide generate_intents(snapshot).
    - Risk rules implement RiskRuleABC and are applied in sequence.

    Dependency Injection
    - Objects receive dependencies via constructors rather than importing them internally.
    - This keeps modules testable and reduces coupling.
    """
    logger.warning("SIGINT handler at start of main: %r", signal.getsignal(signal.SIGINT))

    with log_scope("main", logger):
        # Load variables from a .env file into process environment.
        # This lets you run locally without permanently setting system env vars.
        load_dotenv()

        # Install interrupt handling early so Ctrl+C is visible in logs.
        _install_interrupt_tracer()

        # Runtime config controls which strategies run and their parameters.
        runtime_cfg: RuntimeConfig = load_runtime_config()

        # App config represents broker selection and broker credentials loaded from env.
        app_cfg = load_app_config_from_env()

        # Log configuration snapshot so startup configuration is always auditable.
        _log_env_snapshot()

        # Dry run determines whether orders are actually submitted.
        # TBOT_DRY_RUN overrides runtime default when explicitly set.
        dry_run: bool = _get_env_bool("TBOT_DRY_RUN", default=runtime_cfg.default_dry_run)
        logger.info("Dry run resolved | dry_run=%s", dry_run)

        # Broker is a pluggable component. The selected name comes from AppConfig.
        broker_name: str = app_cfg.broker_name
        logger.info("Broker selected | name=%s", broker_name)

        # Construct broker using registry + facade.
        # This keeps broker-specific wiring out of main() and supports multiple brokers cleanly.
        with log_scope("build_broker", logger, extra=f"name={broker_name}"):
            registry = BrokerBuilderRegistry()
            registry.register("alpaca", AlpacaBrokerBuilder())

            broker_factory = BrokerFactoryFacade(registry=registry)
            broker: BrokerABC = broker_factory.build_broker(app_cfg=app_cfg)

        # Construct risk engine and its injected components.
        risk_engine: RiskEngine = _build_risk_engine()
        logger.info("Risk engine built | type=%s", type(risk_engine).__name__)

        # Construct strategy objects from runtime configuration.
        strategies: list[StrategyABC] = _build_strategies(runtime_cfg)
        logger.info(
            "Strategies built | count=%d types=%s",
            len(strategies),
            [type(s).__name__ for s in strategies],
        )

        # ExecutionPolicy converts risk-approved specs into executable domain orders.
        # Changing execution behaviour means swapping this object, not editing risk logic.
        execution_policy = DefaultExecutionPolicy()

        # TradingOrchestrator coordinates one cycle:
        # - build snapshot (quotes, chains, account state) via snapshot builder
        # - ask strategies for intents
        # - evaluate intents with risk engine
        # - convert approvals into orders via execution policy
        # - submit orders via broker (unless dry_run)
        #
        # The orchestrator is the only component allowed to perform broker IO.
        trading_orchestrator: TradingOrchestrator = TradingOrchestrator(
            broker=broker,
            strategies=strategies,
            risk_engine=risk_engine,
            execution_policy=execution_policy,
            dry_run=dry_run,
            # snapshot_builder= TODO ADD HERE
        )
        logger.info("TradingOrchestrator built | type=%s", type(trading_orchestrator).__name__)

        # Scheduler triggers a single cycle repeatedly.
        # It does not contain trading logic; it only controls timing.
        cycle_seconds: float = float(os.getenv("TBOT_CYCLE_SECONDS", "60"))
        scheduler: Scheduler = Scheduler(
            orchestrator=trading_orchestrator,
            config=SchedulerConfig(cycle_seconds=cycle_seconds),
        )

        logger.info("Start scheduler.run_forever | cycle_seconds=%s", cycle_seconds)
        scheduler.run_forever()


if __name__ == "__main__":
    main()
