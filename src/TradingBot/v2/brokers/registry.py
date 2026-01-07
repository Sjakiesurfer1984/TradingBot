from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

logger = setup_logger("Broker Registry")


@dataclass(frozen=True)
class UnknownBrokerError(ValueError):
    """
    Raised when the user selects a broker name that this codebase does not support.
    """
    broker_name: str
    supported: tuple[str, ...]

    def __str__(self) -> str:
        supported_csv: str = ", ".join(self.supported)
        return f"Unknown broker '{self.broker_name}'. Supported brokers: {supported_csv}."


BrokerFactory = Callable[[], BrokerInterfaceV2]


def resolve_broker(factory_map: Dict[str, BrokerFactory], broker_name: str) -> BrokerInterfaceV2:
    """
    Resolve and build a broker from a name -> factory mapping.
    """
    with log_scope("registry.resolve_broker", logger, extra=f"broker_name={broker_name}"):
        key: str = broker_name.strip().lower()
        logger.info("Broker key resolved | raw=%s key=%s", broker_name, key)

        supported: tuple[str, ...] = tuple(sorted(factory_map.keys()))
        logger.info("Supported brokers | supported=%s", list(supported))

        if key not in factory_map:
            logger.error("Unknown broker requested | broker_name=%s supported=%s", broker_name, list(supported))
            raise UnknownBrokerError(broker_name=broker_name, supported=supported)

        logger.info("Broker factory found | key=%s. Constructing broker.", key)
        broker: BrokerInterfaceV2 = factory_map[key]()
        logger.info("Broker constructed | key=%s type=%s", key, type(broker).__name__)
        return broker
