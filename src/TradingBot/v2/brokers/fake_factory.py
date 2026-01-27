from __future__ import annotations

from TradingBot.v2.brokers.broker_interface import BrokerInterface
from TradingBot.v2.brokers.fake_broker import FakeBroker

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

logger = setup_logger("Fake Broker Factory")


def build_fake_broker() -> BrokerInterface:
    """
    Build FakeBrokerV2 with deterministic values.
    """
    with log_scope("fake_factory.build_fake_broker", logger):
        logger.info("Constructing FakeBrokerV2 with deterministic test values")

        broker: BrokerInterface = FakeBroker(
            options_buying_power=100_000.0,
            equity=100_000.0,
            positions={},
            open_orders=[],
        )

        logger.info(
            "FakeBroker constructed | option_buying_power=%.2f equity=%.2f positions=%d open_orders=%d",
            100_000.0,
            100_000.0,
            0,
            0,
        )

        return broker
