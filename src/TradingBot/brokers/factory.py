"""
factory.py

Purpose
- BrokerFactoryFacade constructs one BrokerABC from AppConfig.
- Contains zero broker-specific wiring.

UML contract
main -> BrokerFactoryFacade -> BrokerBuilderRegistry -> BrokerBuilderABC -> ConcreteBroker
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.brokers.registry import BrokerBuilderRegistryABC
from TradingBot.config.env_config import EnvConfig
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("BrokerFactoryFacade")


class BrokerFactoryFacadeABC(ABC):
    @abstractmethod
    def build_broker(self, *, app_cfg: EnvConfig) -> BrokerABC:
        raise NotImplementedError


@dataclass(frozen=True)
class BrokerFactoryFacade(BrokerFactoryFacadeABC):
    registry: BrokerBuilderRegistryABC

    def build_broker(self, *, app_cfg: EnvConfig) -> BrokerABC:
        broker_name: str = str(app_cfg.broker_name).strip()

        with log_scope("brokers.factory_facade.build_broker", logger, extra=f"broker_name={broker_name}"):
            builder = self.registry.get_builder(broker_name)
            broker: BrokerABC = builder.build(app_cfg)
            logger.info("Broker constructed | broker_name=%s broker_type=%s", broker_name, type(broker).__name__)
            return broker
