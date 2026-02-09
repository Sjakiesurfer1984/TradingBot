"""
factory.py

Purpose
- Construct ONE BrokerABC instance based on AppConfig.
- Factory contains NO broker-specific wiring.
- Broker-specific wiring lives in builder classes registered in registry.py.

UML contract
main -> BrokerFactory -> BrokerRegistry -> BrokerBuilderABC -> ConcreteBuilder -> ConcreteBroker
"""

from __future__ import annotations

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.brokers.registry import get_broker_builder
from TradingBot.config.settings import AppConfig
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("BrokerFactory")


def build_broker(*, app_cfg: AppConfig) -> BrokerABC:
    """
    Build and return the configured broker.

    Contract
    - app_cfg is loaded once at startup via load_app_config_from_env()
    - broker selection uses app_cfg.broker_name
    - registry decides which builder is responsible
    - builder constructs and returns a concrete BrokerABC
    """
    broker_name: str = str(app_cfg.broker_name).strip()

    with log_scope("brokers.factory.build_broker", logger, extra=f"broker_name={broker_name}"):
        logger.info("Resolving broker builder | broker_name=%s", broker_name)

        builder = get_broker_builder(broker_name)

        logger.info(
            "Broker builder resolved | broker_name=%s builder_type=%s",
            broker_name,
            type(builder).__name__,
        )

        broker: BrokerABC = builder.build(app_cfg)

        logger.info(
            "Broker constructed | broker_name=%s broker_type=%s",
            broker_name,
            type(broker).__name__,
        )

        return broker
