from __future__ import annotations

from dataclasses import dataclass

from src.brokers.interfaces import BrokerABC
from src.brokers.registry import BrokerBuilderRegistryABC
from src.config.env_config import EnvConfig
from src.utilities.logger import setup_logger

logger = setup_logger("BrokerFactory")


@dataclass(frozen=True)
class BrokerFactoryFacade:
    registry: BrokerBuilderRegistryABC

    def build_broker(self, *, env_cfg: EnvConfig) -> BrokerABC:
        name = str(env_cfg.broker_name).strip()
        logger.info("Building broker | name=%s", name)
        broker = self.registry.get_builder(name).build(env_cfg)
        logger.info("Broker built | type=%s", type(broker).__name__)
        return broker
