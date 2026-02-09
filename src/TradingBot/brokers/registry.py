"""
registry.py

Purpose
- Single source of truth for supported brokers.
- Maps broker_name -> BrokerBuilderABC.
- Registry does NOT build brokers. Builders do.

UML contract
BrokerFactory -> BrokerRegistry -> BrokerBuilderABC -> ConcreteBuilder -> ConcreteBroker
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.config.settings import AppConfig
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("BrokerRegistry")


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class UnknownBrokerError(ValueError):
    """
    Raised when the configured broker name is not supported by this codebase.
    """
    broker_name: str
    supported: tuple[str, ...]

    def __str__(self) -> str:
        supported_csv: str = ", ".join(self.supported)
        return f"Unknown broker '{self.broker_name}'. Supported brokers: {supported_csv}."


# ---------------------------------------------------------------------
# Builder contract
# ---------------------------------------------------------------------

class BrokerBuilderABC(ABC):
    """
    Builder contract for constructing a concrete BrokerABC.

    Why this exists
    - Broker construction can require multiple config inputs (keys, mode, timeouts).
    - We keep that wiring OUT of factory.py and OUT of main.py.
    """

    @abstractmethod
    def build(self, app_cfg: AppConfig) -> BrokerABC:
        raise NotImplementedError


# ---------------------------------------------------------------------
# Concrete builders
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AlpacaBrokerBuilder(BrokerBuilderABC):
    """
    Constructs AlpacaBroker using AppConfig loaded from env.

    Uses:
    - app_cfg.alpaca.mode ("paper" or "live")
    - app_cfg.alpaca.api_key
    - app_cfg.alpaca.api_secret
    """

    def build(self, app_cfg: AppConfig) -> BrokerABC:
        from TradingBot.brokers.alpaca_broker import AlpacaBroker

        mode: str = str(app_cfg.alpaca.mode).strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError(f"Alpaca mode must be 'paper' or 'live', got '{mode}'")

        api_key: str = str(app_cfg.alpaca.api_key).strip()
        api_secret: str = str(app_cfg.alpaca.api_secret).strip()

        if not api_key or not api_secret:
            raise ValueError("Alpaca API key/secret missing. Check your .env variables.")

        paper: bool = (mode == "paper")

        return AlpacaBroker(
            api_key=api_key,
            api_secret=api_secret,
            paper=paper,
        )

# ---------------------------------------------------------------------
# Registry (single source of truth)
# ---------------------------------------------------------------------

_BROKER_BUILDERS: Dict[str, BrokerBuilderABC] = {
    "alpaca": AlpacaBrokerBuilder(),
}


def get_broker_builder(broker_name: str) -> BrokerBuilderABC:
    """
    Resolve broker_name -> BrokerBuilderABC.

    Registry does not construct brokers.
    """
    with log_scope("brokers.registry.get_broker_builder", logger, extra=f"broker_name={broker_name}"):
        key: str = str(broker_name).strip().lower()

        supported: tuple[str, ...] = tuple(sorted(_BROKER_BUILDERS.keys()))
        builder = _BROKER_BUILDERS.get(key)

        if builder is None:
            logger.error("Unknown broker requested | broker_name=%s supported=%s", broker_name, list(supported))
            raise UnknownBrokerError(broker_name=broker_name, supported=supported)

        logger.info("Broker builder resolved | key=%s builder_type=%s", key, type(builder).__name__)
        return builder
