"""
registry.py

Purpose
- BrokerBuilderRegistry is the single source of truth for broker builders.
- Resolves broker_name -> BrokerBuilderABC.

UML contract
BrokerFactoryFacade -> BrokerBuilderRegistry -> BrokerBuilderABC -> ConcreteBroker
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Tuple

from TradingBot.brokers.broker_interface import BrokerABC

from TradingBot.config.env_config import EnvConfig

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("BrokerRegistry")


@dataclass(frozen=True)
class UnknownBrokerError(ValueError):
    broker_name: str
    supported: Tuple[str, ...]

    def __str__(self) -> str:
        supported_csv: str = ", ".join(self.supported)
        return f"Unknown broker '{self.broker_name}'. Supported brokers: {supported_csv}."


class BrokerBuilderABC(ABC):
    @abstractmethod
    def build(self, app_cfg: EnvConfig) -> BrokerABC:
        raise NotImplementedError


class BrokerBuilderRegistryABC(ABC):
    @abstractmethod
    def register(self, broker_name: str, builder: BrokerBuilderABC) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_builder(self, broker_name: str) -> BrokerBuilderABC:
        raise NotImplementedError

    @abstractmethod
    def supported(self) -> Tuple[str, ...]:
        raise NotImplementedError


class BrokerBuilderRegistry(BrokerBuilderRegistryABC):
    def __init__(self) -> None:
        self._builders: Dict[str, BrokerBuilderABC] = {}

    @staticmethod
    def _norm(name: str) -> str:
        return str(name).strip().lower()

    def register(self, broker_name: str, builder: BrokerBuilderABC) -> None:
        key: str = self._norm(broker_name)
        self._builders[key] = builder

    def supported(self) -> Tuple[str, ...]:
        return tuple(sorted(self._builders.keys()))

    def get_builder(self, broker_name: str) -> BrokerBuilderABC:
        with log_scope("brokers.registry.get_builder", logger, extra=f"broker_name={broker_name}"):
            key: str = self._norm(broker_name)
            builder = self._builders.get(key)

            if builder is None:
                supported: Tuple[str, ...] = self.supported()
                raise UnknownBrokerError(broker_name=broker_name, supported=supported)

            logger.info("Broker builder resolved | key=%s builder_type=%s", key, type(builder).__name__)
            return builder


class AlpacaBrokerBuilder(BrokerBuilderABC):
    def build(self, app_cfg: EnvConfig) -> BrokerABC:
        mode: str = str(app_cfg.alpaca.mode).strip().lower()
        api_key: str = str(app_cfg.alpaca.api_key).strip()
        api_secret: str = str(app_cfg.alpaca.api_secret).strip()

        from TradingBot.brokers.alpaca_broker import AlpacaBroker

        mode: str = str(app_cfg.alpaca.mode).strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError(f"Alpaca mode must be 'paper' or 'live', got '{mode}'")

        api_key: str = str(app_cfg.alpaca.api_key).strip()
        api_secret: str = str(app_cfg.alpaca.api_secret).strip()

        if not api_key or not api_secret:
            raise ValueError("Alpaca API key/secret missing. Check your env variables.")

        paper: bool = (mode == "paper")

        return AlpacaBroker(
            api_key=api_key,
            api_secret=api_secret,
            paper=paper,
        )
