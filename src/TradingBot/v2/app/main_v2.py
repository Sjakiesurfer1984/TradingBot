from __future__ import annotations

import os
from typing import List, Optional

from TradingBot.v2.brokers.alpaca_broker_v2 import AlpacaBrokerV2
from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2
from TradingBot.v2.brokers.fake_broker_v2 import FakeBrokerV2
from TradingBot.v2.logger import setup_logger
from TradingBot.v2.orchestrator import OrchestratorV2
from TradingBot.v2.risk.risk_engine import RiskEngineV2
from TradingBot.v2.risk.rules.open_order_rule import OpenOrderDedupeRule
from TradingBot.v2.strategies.pmcc_strategy_v2 import PmccStrategyV2
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2


# Module-level logger.
logger = setup_logger("Main")


def _get_env(name: str) -> Optional[str]:
    """
    Read an environment variable safely.

    Why this exists
    - Secrets must not be hard-coded.
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


def _build_broker(use_real_broker: bool) -> BrokerInterfaceV2:
    """
    Build the broker implementation used by the orchestrator.

    Why this exists
    - Option C requires the orchestrator to be the only layer that does broker IO.
    - We swap broker implementations cleanly:
        - FakeBrokerV2 for dry-run development and tests
        - AlpacaBrokerV2 for real connectivity
    """
    if not use_real_broker:
        # FakeBrokerV2 returns deterministic data and performs no network IO.
        return FakeBrokerV2(
            option_buying_power=100_000.0,
            equity=100_000.0,
            prices={"SPY": 500.0},
            positions={},
            open_orders=[],
        )

    # Real broker configuration is pulled from environment variables.
    api_key: Optional[str] = _get_env("ALPACA_API_KEY")
    api_secret: Optional[str] = _get_env("ALPACA_API_SECRET")
    paper: bool = _get_env_bool("ALPACA_PAPER", default=True)

    if api_key is None or api_secret is None:
        raise ValueError(
            "Missing Alpaca credentials. Set ALPACA_API_KEY and ALPACA_API_SECRET in your environment."
        )

    return AlpacaBrokerV2(
        api_key=api_key,
        api_secret=api_secret,
        paper=paper,
        request_timeout_seconds=10.0,
    )


def main() -> None:
    """
    V2 entry point.

    Default behaviour
    - dry_run True: never submit orders
    - use_real_broker False: avoid network IO during development

    You can override via environment variables
    - TBOT_DRY_RUN=true/false
    - TBOT_USE_REAL_BROKER=true/false
    """
    dry_run: bool = _get_env_bool("TBOT_DRY_RUN", default=True)
    use_real_broker: bool = _get_env_bool("TBOT_USE_REAL_BROKER", default=False)

    try:
        broker: BrokerInterfaceV2 = _build_broker(use_real_broker=use_real_broker)
    except Exception as exc:
        # Entry-point logging belongs here so failures are always visible.
        logger.exception("Failed to build broker: %s", exc)
        return

    # Risk rules (start small, compose later).
    dedupe_rule: OpenOrderDedupeRule = OpenOrderDedupeRule()

    # Risk engine is pure and evaluates intents against rules.
    risk_engine: RiskEngineV2 = RiskEngineV2(dedupe_rule=dedupe_rule)

    # Strategies list is ordered for deterministic behaviour.
    strategies: List[StrategyV2] = [
        PmccStrategyV2(underlying_symbol="SPY"),
    ]

    # Orchestrator owns all broker IO and runs the cycle.
    orchestrator: OrchestratorV2 = OrchestratorV2(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        dry_run=dry_run,
    )

    orchestrator.run_cycle()


if __name__ == "__main__":
    main()
