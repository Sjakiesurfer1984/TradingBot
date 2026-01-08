from __future__ import annotations

import os
import signal
import time
from threading import Event
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

from threading import Event
# PMCC sizing imports
from TradingBot.v2.pmcc_sizer import PmccSizingConfig, PmccSizer

# Module-level logger.
logger = setup_logger("Main")

# Function to read environment variables safely from the .env file or system environment.
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

# function to read environment variables as booleans.
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

# function to mask secrets (Keys, passwords) for logging.
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

# function to log a snapshot of environment variables safely. This function collects all relevant environment variables,
# masks sensitive information, and logs the configuration snapshot for debugging purposes.
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


_STOP_REQUESTED = Event()
_LAST_SIGINT_TS: float = 0.0

def _install_interrupt_tracer() -> None:
    def _handler(signum: int, frame) -> None:
        global _LAST_SIGINT_TS
        now: float = time.time()

        logger.error("Interrupt received | signum=%s time=%s", signum, now)

        if _STOP_REQUESTED.is_set() and (now - _LAST_SIGINT_TS) < 2.0:
            logger.critical("Second interrupt received quickly; exiting immediately.")
            raise KeyboardInterrupt  # ok as a deliberate "force quit"

        _LAST_SIGINT_TS = now
        _STOP_REQUESTED.set()
        logger.warning("Stop requested. Press Ctrl+C again to force quit.")

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handler)

def _build_risk_engine() -> RiskEngineV2:
    with log_scope("build_risk_engine", logger):
        logger.info("Constructing risk rule OpenOrderDedupeRule")
        dedupe_rule: OpenOrderDedupeRule = OpenOrderDedupeRule()

        # Load PMCC sizing configuration from environment variables (defaults to zero / safe fallback).
        cfg = PmccSizingConfig(
            equity_budget_pct=float(os.getenv("TBOT_PMCC_EQUITY_BUDGET_PCT", "0")),
            max_option_bp_fraction=float(os.getenv("TBOT_PMCC_MAX_OPTION_BP_FRACTION", "0")),
            max_debit_per_spread_usd=float(os.getenv("TBOT_PMCC_MAX_DEBIT_PER_SPREAD_USD", "0")),
            max_contracts_per_intent=int(os.getenv("TBOT_PMCC_MAX_CONTRACTS_PER_INTENT", "0")),
            slippage_factor=float(os.getenv("TBOT_PMCC_SLIPPAGE_FACTOR", "1.0")),
            max_leap_spread_pct=float(os.getenv("TBOT_PMCC_MAX_LEAP_SPREAD_PCT", "0")),
            max_near_spread_pct=float(os.getenv("TBOT_PMCC_MAX_NEAR_SPREAD_PCT", "0")),
        )
        pmcc_sizer = PmccSizer(cfg)

        logger.info("Constructing RiskEngineV2 with PMCC sizer")
        risk_engine: RiskEngineV2 = RiskEngineV2(
            dedupe_rule=dedupe_rule,
            pmcc_sizer=pmcc_sizer,
        )

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
    # an orchestrator object is a collection of strategies, a broker, and a risk engine objects.
    broker: BrokerInterfaceV2, # here we pass the broker object to the orchestrator. 
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
            dry_run=dry_run, # this raises an error IF the dry
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
    logger.warning("SIGINT handler at start of main: %r", signal.getsignal(signal.SIGINT))

    with log_scope("main", logger):
        logger.info("Loading .env via python-dotenv")
        # We load the .env file here to ensure all environment variables are set before we read them.
        # the .env file contains sensitive information like API keys, so we must load it before we read any environment variables.
        load_dotenv()

        # _install_interrupt_tracer() must happen after the logger is set up so we can log stack traces.
        # Commented out because we do not need it right now. We used it to track a Ctrl+C issue, which
        # came from signal handling in VSCode. By running the main_v2.py directly from the command terminal, we avoided the problem.
        # _install_interrupt_tracer()

        # we log the environment snapshot for debugging purposes. This snapshot contains all relevant environment variables,
        # with sensitive information masked.
        _log_env_snapshot()
        # retreive the broker we wish to use from the environment variable TRADINGBOT_BROKER.
        broker_name: str = os.getenv("TRADINGBOT_BROKER", "fake")
        logger.info("Building broker | name=%s", broker_name)

        with log_scope("build_broker", logger, extra=f"name={broker_name}"):
            # Here we build the broker using the factory.py "build_broker" function and passing in the broker name (Alpaca, Fake, etc.)
            broker: BrokerInterfaceV2 = build_broker(broker_name)
        # Dry_run is set in the environment variable TBOT_DRY_RUN.
        # If missing, we default to True for safety.
        dry_run: bool = _get_env_bool("TBOT_DRY_RUN", default=True)
        logger.info("Dry run resolved | dry_run=%s", dry_run)
        # The risk engine is responsible for managing risks such as order deduplication, position sizing, etc.
        # We build it using the _build_risk_engine function defined in this file, which in turn calls the RiskEngineV2 constructor.
        risk_engine: RiskEngineV2 = _build_risk_engine()
        logger.info("Risk engine built | type=%s", type(risk_engine).__name__)
        # Build strategies list. Build_strategies function defines which strategies to use, and is defined in this file.
        # build_strategies function in turn calls the constructors of each strategy we wish to use (e.g., PmccStrategyV2).
        strategies: List[StrategyV2] = _build_strategies()
        logger.info("Strategies built | count=%d types=%s", len(strategies), [type(s).__name__ for s in strategies])
        #build_orchestrator function builds the orchestrator with the given broker, strategies, risk engine, and dry run flag. it is defined in this file.
        # The _build_orchestrator function in turn calls the OrchestratorV2 constructor, and injects the following dependencies:
        # - broker: BrokerInterfaceV2
        # - strategies: List[StrategyV2]
        # - risk_engine: RiskEngineV2
        # - dry_run: bool NOTE: this flag tells the orchestrator whether to actually submit orders or not. However, 
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
            if _STOP_REQUESTED.is_set():
                logger.warning("Stop requested, exiting cleanly")
                return
            logger.info("orchestrator.run_cycle completed successfully")
        # except KeyboardInterrupt:
        #     logger.exception("KeyboardInterrupt reached main")
        #     raise
        except Exception:
            logger.exception("orchestrator.run_cycle raised an exception")
            raise
                
        logger.info("End main")
    

if __name__ == "__main__":
    main()
