from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from src.domain.intents import TradeIntent
from src.domain.types import OptionChainRequest, Symbol
from src.orchestration.cycle_snapshot import CycleSnapshotABC


class StrategyABC(ABC):
    @abstractmethod
    def get_symbols(self) -> List[Symbol]:
        raise NotImplementedError

    @abstractmethod
    def generate_intents(self, snapshot: CycleSnapshotABC) -> List[TradeIntent]:
        raise NotImplementedError


class OptionChainConsumerABC(ABC):
    """Mixin for strategies that need option chain data."""

    @abstractmethod
    def get_option_chain_requests(self, snapshot: CycleSnapshotABC) -> List[OptionChainRequest]:
        raise NotImplementedError
