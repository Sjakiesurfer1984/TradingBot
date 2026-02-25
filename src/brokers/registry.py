from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional

from src.brokers.interfaces import BrokerABC
from src.config.env_config import EnvConfig
from src.utilities.logger import setup_logger

logger = setup_logger("BrokerRegistry")


class BrokerBuilderABC(ABC):
    @abstractmethod
    def build(self, env_cfg: EnvConfig) -> BrokerABC:
        raise NotImplementedError


class BrokerBuilderRegistryABC(ABC):
    @abstractmethod
    def register(self, name: str, builder: BrokerBuilderABC) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_builder(self, name: str) -> BrokerBuilderABC:
        raise NotImplementedError


@dataclass
class BrokerBuilderRegistry(BrokerBuilderRegistryABC):
    _builders: Dict[str, BrokerBuilderABC] = field(default_factory=dict, init=False)

    def register(self, name: str, builder: BrokerBuilderABC) -> None:
        self._builders[name.lower().strip()] = builder
        logger.info("Registered broker builder | name=%s", name)

    def get_builder(self, name: str) -> BrokerBuilderABC:
        key = name.lower().strip()
        if key not in self._builders:
            raise KeyError(f"No broker builder for '{name}'. Registered: {list(self._builders)}")
        return self._builders[key]


def _env(name: str) -> Optional[str]:
    v = os.getenv(name, "").strip()
    return v or None


def _mask(v: Optional[str], n: int = 4) -> str:
    if not v:
        return "<missing>"
    return v[:n] + "*" * max(0, len(v) - n)


class AlpacaBrokerBuilder(BrokerBuilderABC):
    """
    Builds AlpacaBroker directly from environment variables.

    Reads:
      ALPACA_PAPER_API_KEY    / ALPACA_LIVE_API_KEY
      ALPACA_PAPER_API_SECRET / ALPACA_LIVE_API_SECRET
      env_cfg.alpaca.mode     ("paper" | "live")

    alpaca_factory.py has been deleted — construction lives here.
    """

    def build(self, env_cfg: EnvConfig) -> BrokerABC:
        from src.brokers.alpaca_broker import AlpacaBroker
        from src.utilities.clock import LiveClock

        paper = env_cfg.alpaca.mode != "live"

        if paper:
            api_key    = _env("ALPACA_PAPER_API_KEY")
            api_secret = _env("ALPACA_PAPER_API_SECRET")
        else:
            api_key    = _env("ALPACA_LIVE_API_KEY")
            api_secret = _env("ALPACA_LIVE_API_SECRET")

        if not api_key or not api_secret:
            prefix = "ALPACA_PAPER" if paper else "ALPACA_LIVE"
            raise EnvironmentError(
                f"Missing Alpaca credentials for mode='{env_cfg.alpaca.mode}'. "
                f"Set {prefix}_API_KEY and {prefix}_API_SECRET in your .env file."
            )

        logger.info(
            "Building AlpacaBroker | mode=%s key=%s",
            env_cfg.alpaca.mode, _mask(api_key),
        )

        return AlpacaBroker(
            api_key=api_key,
            secret_key=api_secret,
            paper=paper,
            clock=LiveClock(),
        )