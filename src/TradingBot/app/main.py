# src/TradingBot/v2/main.py
from __future__ import annotations

import os
import signal
import time
from threading import Event
from typing import Optional

from dotenv import load_dotenv

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.brokers.factory import build_broker

from TradingBot.config.runtime_config import RuntimeConfig, load_runtime_config
from TradingBot.config.settings import load_app_config_from_env

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

from TradingBot.orchestration.orchestrator import Orchestrator
from TradingBot.risk.pmcc_sizer import PmccSizer, PmccSizingConfig
from TradingBot.risk.risk_engine import RiskEngine
from TradingBot.risk.rules.open_order_rule import OpenOrderDedupeRule

from TradingBot.strategies.pmcc_strategy import PmccStrategy
from TradingBot.strategies.strategy_interface import Strategy

from TradingBot.app.scheduler import SchedulerConfig, Scheduler


logger = setup_logger("Main")

# The _STOP_REQUESTED event is set when a SIGINT is received, and serves as a signal to gracefully stop operations.
#_LAST_SIGINT_TS records the timestamp of the last SIGINT to handle double interrupts.
# SIGINT is a keyboard interrupt (Ctrl+C).

_STOP_REQUESTED = Event()
_LAST_SIGINT_TS: float = 0.0


def _get_env(name: str) -> Optional[str]:
    """
    Read an environment variable safely.

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
    with log_scope("build_risk_engine", logger):
        dedupe_rule: OpenOrderDedupeRule = OpenOrderDedupeRule()

        cfg = PmccSizingConfig(
            equity_budget_pct=float(os.getenv("TBOT_PMCC_EQUITY_BUDGET_PCT", "0")),
            max_option_bp_fraction=float(os.getenv("TBOT_PMCC_MAX_OPTION_BP_FRACTION", "0")),
            max_debit_per_spread_usd=float(os.getenv("TBOT_PMCC_MAX_DEBIT_PER_SPREAD_USD", "0")),
            max_contracts_per_intent=int(os.getenv("TBOT_PMCC_MAX_CONTRACTS_PER_INTENT", "0")),
            slippage_factor=float(os.getenv("TBOT_PMCC_SLIPPAGE_FACTOR", "1.0")),
            max_leap_spread_pct=float(os.getenv("TBOT_PMCC_MAX_LEAP_SPREAD_PCT", "0")),
            max_near_spread_pct=float(os.getenv("TBOT_PMCC_MAX_NEAR_SPREAD_PCT", "0")),
            ignore_spread_checks=_get_env_bool("TBOT_PMCC_IGNORE_SPREAD_CHECKS", default=False),
        )

        pmcc_sizer: PmccSizer = PmccSizer(cfg)

        risk_engine: RiskEngine = RiskEngine(
            dedupe_rule=dedupe_rule,
            pmcc_sizer=pmcc_sizer,
        )
        return risk_engine


def _build_strategies(runtime_cfg: RuntimeConfig) -> list[Strategy]:
    strategies: list[Strategy] = []

    for spec in runtime_cfg.strategies:
        if spec.name == "pmcc":
            if spec.pmcc is None:
                raise ValueError("PMCC StrategySpec missing pmcc config")

            cfg = spec.pmcc
            strategies.append(PmccStrategy(underlying_symbol=cfg.underlying_symbol))
            continue

        raise ValueError(f"Unsupported strategy name: {spec.name}")

    return strategies


def main() -> None:
    """
    Entry point.
    High-level flow:
    - Load config from env / .env.
    - load runtime config.
    - load app config.
    - Build one broker instance.
    - Inject that broker into the orchestrator.
    - Orchestrator is the only component allowed to perform broker IO.
    """
    logger.warning("SIGINT handler at start of main: %r", signal.getsignal(signal.SIGINT))

    with log_scope("main", logger):
        load_dotenv()

        runtime_cfg: RuntimeConfig = load_runtime_config()
        app_cfg = load_app_config_from_env()

        _log_env_snapshot()

        # If TBOT_DRY_RUN is set, it overrides the runtime default.
        dry_run: bool = _get_env_bool("TBOT_DRY_RUN", default=runtime_cfg.default_dry_run)
        logger.info("Dry run resolved | dry_run=%s", dry_run)

        broker_name: str = app_cfg.broker_name
        logger.info("Broker selected | name=%s", broker_name)

        with log_scope("build_broker", logger, extra=f"name={broker_name}"):
            broker:  BrokerABC = build_broker(app_cfg=app_cfg)

        risk_engine: RiskEngine = _build_risk_engine()
        logger.info("Risk engine built | type=%s", type(risk_engine).__name__)

        strategies: list[Strategy] = _build_strategies(runtime_cfg)
        logger.info(
            "Strategies built | count=%d types=%s",
            len(strategies),
            [type(s).__name__ for s in strategies],
        )

        # Option B: Orchestrator owns the IO boundary via the single broker object.
        orchestrator: Orchestrator = Orchestrator(
            broker=broker,
            strategies=strategies,
            risk_engine=risk_engine,
            dry_run=dry_run,
        )
        logger.info("Orchestrator built | type=%s", type(orchestrator).__name__)

        cycle_seconds: float = float(os.getenv("TBOT_CYCLE_SECONDS", "60"))
        scheduler: Scheduler = Scheduler(
            orchestrator=orchestrator,
            config=SchedulerConfig(cycle_seconds=cycle_seconds),
        )

        logger.info("Start scheduler.run_forever | cycle_seconds=%s", cycle_seconds)
        scheduler.run_forever()

if __name__ == "__main__":
    main()
