from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict

from src.brokers.interfaces import BrokerABC
from src.config.env_config import EnvConfig
from src.utilities.logger import setup_logger

logger = setup_logger("BrokerRegistry")


class BrokerBuilderABC(ABC):
    """
    Abstract Broker Builder.

    Purpose:
    - Defines the contract for "how to build a BrokerABC" given EnvConfig.
    - This is the plug-in interface for new brokers.

    Key idea:
    - Code that needs a broker should depend on BrokerABC.
    - Code that chooses which broker to construct should depend on BrokerBuilderABC,
      not on AlpacaBroker, IbkrBroker, etc.

    Why an ABC here:
    - It forces every broker integration to provide a consistent build(env_cfg) entry point.
    - It prevents leaking broker-specific constructor details into the rest of the application.
    """

    @abstractmethod
    def build(self, env_cfg: EnvConfig) -> BrokerABC:
        """
        Build and return a concrete broker implementation that satisfies BrokerABC.

        env_cfg:
        - Contains environment-driven settings such as broker name, api keys, mode, etc.

        Returns:
        - A fully configured BrokerABC (for example AlpacaBroker).
        """
        raise NotImplementedError


class BrokerBuilderRegistryABC(ABC):
    """
    Abstract Registry interface.

    Purpose:
    - Defines how builders are stored and retrieved.
    - Allows you to swap out the registry implementation later (for example, add validation,
      allow aliases, support lazy registration, support discovery) without rewriting callers.

    The registry is a map:
    - broker_name (string key) -> BrokerBuilderABC (builder instance)
    """

    @abstractmethod
    def register(self, name: str, builder: BrokerBuilderABC) -> None:
        """
        Register a builder under a broker name.

        Example:
        - register("alpaca", AlpacaBrokerBuilder())
        """
        raise NotImplementedError

    @abstractmethod
    def get_builder(self, name: str) -> BrokerBuilderABC:
        """
        Lookup and return the builder for the given broker name.

        The caller typically provides the value from configuration, for example:
        - env_cfg.broker_name
        """
        raise NotImplementedError


@dataclass
class BrokerBuilderRegistry(BrokerBuilderRegistryABC):
    """
    Concrete in-memory registry.

    How it is used:
    - Startup code registers supported brokers (alpaca, ibkr, etc.).
    - A factory (often called BrokerFactoryFacade) retrieves the correct builder based on config.
    - The builder constructs the actual BrokerABC instance.

    Why keep builders in a registry instead of if/elif?
    - It avoids a growing conditional chain in your factory or main entry point.
    - New brokers become a small, isolated change: implement a builder + register it.
    - The application remains open for extension but closed for modification (OCP).
    """

    _builders: Dict[str, BrokerBuilderABC] = field(default_factory=dict, init=False)

    def register(self, name: str, builder: BrokerBuilderABC) -> None:
        """
        Store the builder using a normalised (lowercased, stripped) key.

        This normalisation reduces configuration fragility:
        - "Alpaca", " alpaca ", "ALPACA" all map to the same entry.
        """
        self._builders[name.lower().strip()] = builder
        logger.info("Registered broker builder | name=%s", name)

    def get_builder(self, name: str) -> BrokerBuilderABC:
        """
        Retrieve a builder by broker name.

        If the name is not registered:
        - Raise KeyError with helpful diagnostics (show registered keys).
        """
        key = name.lower().strip()
        if key not in self._builders:
            raise KeyError(f"No broker builder for '{name}'. Registered: {list(self._builders)}")
        return self._builders[key]


class AlpacaBrokerBuilder(BrokerBuilderABC):
    """
    Concrete builder for Alpaca.

    Responsibility:
    - Knows how to construct an Alpaca broker instance.
    - Contains the only direct dependency on alpaca construction code (alpaca_factory).

    Important design rule:
    - The rest of the codebase should not import alpaca_factory directly.
    - Only this builder (and Alpaca-specific modules) should know about Alpaca construction.

    Cooperation with a factory facade (typical flow):
    - A factory reads env_cfg.broker_name (for example "alpaca").
    - It asks the registry for the builder registered under that name.
    - It calls builder.build(env_cfg) to create a BrokerABC.
    - The returned broker is injected into the orchestrator and used via BrokerABC only.
    """

    def build(self, env_cfg: EnvConfig) -> BrokerABC:
        """
        Build and return an AlpacaBroker.

        Note:
        - This delegates to a factory function (build_alpaca_broker) which typically:
          - reads env vars or uses env_cfg to obtain credentials
          - configures paper/live mode
          - returns a fully initialised AlpacaBroker adapter
        """
        from src.brokers.alpaca_factory import build_alpaca_broker
        return build_alpaca_broker()
