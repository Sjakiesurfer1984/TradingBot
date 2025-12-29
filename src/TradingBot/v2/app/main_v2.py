from __future__ import annotations

from typing import List

from TradingBot.config import ALPACA_CONFIG, validate_alpaca_config
from TradingBot.logger import setup_logger

from TradingBot.v2.orchestrator import OrchestratorV2
from TradingBot.v2.risk.risk_engine import RiskEngineV2
from TradingBot.v2.rules.open_order_rule import OpenOrderDedupeRule
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2
from TradingBot.v2.strategies.pmcc_strategy_v2 import PmccStrategyV2

from TradingBot.v2.brokers.fake_broker_v2 import FakeBroker
from TradingBot.v2.brokers.alpaca_broker_v2 import AlpacaBrokerV2

logger = setup_logger("MainV2")


def main() -> None:
    """
    V2 entry point.

    Behaviour
    - Defaults to a fake broker so Option C can be validated without network IO.
    - Allows switching to AlpacaBrokerV2 once the broker adapter is confirmed non-blocking.
    """

    # Safety switch.
    # Keep this True until:
    # - sizing exists in the risk engine
    # - orchestrator implements submission for approved orders
    dry_run: bool = True

    # Broker selection switch.
    # Start with FakeBroker to validate the architecture.
    # Switch to AlpacaBrokerV2 only when you want to test real connectivity.
    use_real_broker: bool = False

    if use_real_broker:
        try:
            validate_alpaca_config(ALPACA_CONFIG)
        except ValueError as exc:
            logger.error(f"Configuration validation error: {exc}")
            return

        broker = AlpacaBrokerV2(
            api_key=ALPACA_CONFIG["api_key"],
            api_secret=ALPACA_CONFIG["api_secret"],
            paper=ALPACA_CONFIG["paper"],
            request_timeout_seconds=10.0,
        )
    else:
        broker = FakeBroker(
            option_buying_power=100_000.0,
            equity=100_000.0,
            prices={"SPY": 500.0},
            positions={},
            open_orders=[],
        )

    dedupe_rule = OpenOrderDedupeRule()
    risk_engine = RiskEngineV2(dedupe_rule=dedupe_rule)

    strategies: List[StrategyV2] = [
        PmccStrategyV2(underlying_symbol="SPY"),
    ]

    orchestrator = OrchestratorV2(
        broker=broker,
        strategies=strategies,
        risk_engine=risk_engine,
        dry_run=dry_run,
    )

    orchestrator.run_cycle()


if __name__ == "__main__":
    main()
