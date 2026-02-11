from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Set

from TradingBot.domain.intents import TradeIntent
from TradingBot.domain.types import StrategyId, Symbol
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC


class StrategyABC(ABC):
    """
    Strategy contract (UML).

    A Strategy:
    - consumes a CycleSnapshotABC snapshot
    - produces a list of TradeIntent objects
    - performs no broker IO
    - declares its universe explicitly
    """

    @property
    @abstractmethod
    def strategy_id(self) -> StrategyId:
        raise NotImplementedError

    @abstractmethod
    def universe(self) -> Set[Symbol]:
        raise NotImplementedError

    @abstractmethod
    def generate_intents(self, snapshot: CycleSnapshotABC) -> list[TradeIntent]:
        raise NotImplementedError
