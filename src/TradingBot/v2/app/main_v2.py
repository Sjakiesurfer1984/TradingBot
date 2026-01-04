from __future__ import annotations

import os
import signal
import threading
import traceback
from typing import List, Optional  # Optional is used in _get_env function.
# It is used to annotate variables and function return types that may either hold a value of a specified type or be None.
# In other words, it is optional to provide a value for that variable or return type.

from dotenv import load_dotenv

from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2
from TradingBot.v2.brokers.factory import build_broker
from TradingBot.v2.logger import setup_logger
from TradingBot.v2.orchestrator import OrchestratorV2
from TradingBot.v2.risk.risk_engine import RiskEngineV2
from TradingBot.v2.risk.rules.open_order_rule import OpenOrderDedupeRule
from TradingBot.v2.strategies.pmcc_strategy_v2 import PmccStrategyV2
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2

from TradingBot.v2.logging_utils import log_scope

# Module-level logger.
logger = setup_logger("Main")


def _get_env(name: str) -> Optional[str]:
    """
    Read an environment variable safely.

    Why this exists
    - Secrets and keys must not be hard-coded.
    - Environment variables are standard for local dev, CI, and deployments.

    Return value
    - The stripped string value if present and non-empty
    - None if missing or empty
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
    """
    raw: Optional[str] = _get_env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y"}


def _mask_secret(value: Optional[str], show_prefix: int = 4) -> str:
    """
    Mask secrets in logs.

    Why this exists
    - Logs must never leak API keys.
    - We still want enough information to confirm which key is in use.
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
    - When something fails, we need to confirm which execution mode we are in.
    - We must not print secrets, only masked values.
    """
    broker_name: str = os.getenv("TRADINGBOT_BROKER", "fake")

    dry_run_raw: Optional[str] = _get_env("TBOT_DRY_RUN")
    use_real_raw: Optional[str] = _get_env("TBOT_USE_REAL_BROKER")

    alpaca_mode: Optional[str] = _get_env("ALPACA_MODE")

    paper_key: Optional[str] = _get_env("ALPACA_PAPER_API_KEY")
    paper_secret: Optional[str] = _get_env("ALPACA_PAPER_API_SECRET")
    live_key: Optional[str] = _get_env("ALPACA_LIVE_API_KEY")
    live_secret: Optional[str] = _get_env("ALPACA_LIVE_API_SECRET")

    logger.info("Config snapshot begin")
    logger.info("TRADINGBOT_BROKER=%s", broker_name)
    logger.info("TBOT_DRY_RUN=%s", dry_run_raw if dry_run_raw is not None else "<default True>")
    logger.info("TBOT_USE_REAL_BROKER=%s", use_real_raw if use_real_raw is not None else "<unused in this entrypoint>")
    logger.info("ALPACA_MODE=%s", alpaca_mode if alpaca_mode is not None else "<default paper>")

    logger.info("ALPACA_PAPER_API_KEY=%s", _mask_secret(paper_key))
    logger.info("ALPACA_PAPER_API_SECRET=%s", _mask_secret(paper_secret))
    logger.info("ALPACA_LIVE_API_KEY=%s", _mask_secret(live_key))
    logger.info("ALPACA_LIVE_API_SECRET=%s", _mask_secret(live_secret))
    logger.info("Config snapshot end")


def _install_interrupt_tracer() -> None:
    """
    Install a SIGINT/SIGBREAK handler that logs when an interrupt arrives.

    Why this exists
    - KeyboardInterrupt is not a network error.
    - It happens only when the process receives an interrupt control event.
    - Logging the stack here proves exactly where execution was interrupted.
    """

    def _handler(signum: int, frame) -> None:
        stack: str = "".join(traceback.format_stack(frame))
        logger.error(
            "Interrupt received | signum=%s pid=%s thread=%s\nStack:\n%s",
            signum,
            os.getpid(),
            threading.current_thread().name,
            stack,
        )
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handler)

    # Windows supports SIGBREAK (Ctrl+Break), which can also trigger interruptions.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handler)


def _build_risk_engine() -> RiskEngineV2:
    with log_scope("build_risk_engine", logger):
        logger.info("Constructing risk rule OpenOrderDedupeRule")
        dedupe_rule: OpenOrderDedupeRule = OpenOrderDedupeRule()

        logger.info("Constructing RiskEngineV2")
        risk_engine: RiskEngineV2 = RiskEngineV2(dedupe_rule=dedupe_rule)

        logger.info("Risk engine constructed | type=%s", type(risk_engine).__name__)
        return risk_engine


def _build_strategies() -> List[StrategyV2]:
    with log_scope("build_strategies", logger):
        logger.info("Constructing strategies list")
        strategies: List[StrategyV2] = [
            PmccStrategyV2(underlying_symbol="SPY"),
        ]
        logger.info("Strategies constructed count=%d types=%s", len(strategies), [type(s).__name__ for s in strategies])
        return strategies


def _build_orchestrator(
    broker: BrokerInterfaceV2, # Why are we giving it a broker ABC, not a concrete broker?
    strategies: List[StrategyV2],
    risk_engine: RiskEngineV2,
    dry_run: bool,
) -> OrchestratorV2:
    with log_scope("build_orchestrator", logger):
        logger.info("Constructing OrchestratorV2")
        orchestrator: OrchestratorV2 = OrchestratorV2(
            broker=broker,
            strategies=strategies,
            risk_engine=risk_engine,
            dry_run=dry_run,
        )
        logger.info("Orchestrator constructed | type=%s", type(orchestrator).__name__)
        return orchestrator

# ---------------------------------------------------------------------
# Start the main loop
# ---------------------------------------------------------------------
def main() -> None:
    """
    V2 entry point.

    Default behaviour
    - dry_run True: never submit orders
    - broker defaults to "fake" unless TRADINGBOT_BROKER says otherwise
    """
    print("RUNNING FILE:", __file__)
    with log_scope("main", logger):
        logger.info("Loading .env via python-dotenv")
        load_dotenv()

        # This must happen after the logger is set up so we can log stack traces.
        _install_interrupt_tracer()

        _log_env_snapshot()

        broker_name: str = os.getenv("TRADINGBOT_BROKER", "fake")
        logger.info("Building broker | name=%s", broker_name)

        with log_scope("build_broker", logger, extra=f"name={broker_name}"):
            broker: BrokerInterfaceV2 = build_broker(broker_name)

        dry_run: bool = _get_env_bool("TBOT_DRY_RUN", default=True)
        logger.info("Dry run resolved | dry_run=%s", dry_run)

        risk_engine: RiskEngineV2 = _build_risk_engine()
        logger.info("Risk engine built | type=%s", type(risk_engine).__name__)

        strategies: List[StrategyV2] = _build_strategies()
        logger.info("Strategies built | count=%d types=%s", len(strategies), [type(s).__name__ for s in strategies])

        orchestrator: OrchestratorV2 = _build_orchestrator(
            broker=broker,
            strategies=strategies,
            risk_engine=risk_engine,
            dry_run=dry_run,
        )
        logger.info("Orchestrator built | type=%s", type(orchestrator).__name__)

        logger.info("Start orchestrator.run_cycle")
        try:
            with log_scope("orchestrator.run_cycle", logger):
                orchestrator.run_cycle()
            logger.info("orchestrator.run_cycle completed successfully")
        except KeyboardInterrupt:
            logger.exception("KeyboardInterrupt reached main")
            raise
        except Exception:
            logger.exception("orchestrator.run_cycle raised an exception")
            raise
                
        logger.info("End main")
    


if __name__ == "__main__":
    main()
