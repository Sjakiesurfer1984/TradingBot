from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from TradingBot.domain.types import OptionChainRequest


class OptionChainConsumerABC(ABC):
    """
    Optional strategy capability.

    Contract
    - Strategy declares option chain requests.
    - Orchestrator (or snapshot builder) performs broker IO once per cycle.
    """

    @abstractmethod
    def option_chain_requests(self) -> List[OptionChainRequest]:
        raise NotImplementedError
