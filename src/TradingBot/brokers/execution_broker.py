from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List


class ExecutionBrokerABC(ABC):
    """
    Execution contract.

    Why this exists:
    - Keeps interface segregation: execution is separate from market data.
    - Enforces strict behaviour through ABC, not Protocol.
    """

    @abstractmethod
    def submit_order(self, order: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_account(self) -> Any:
        raise NotImplementedError

    @abstractmethod
    def list_open_orders(self) -> List[Any]:
        raise NotImplementedError

    @abstractmethod
    def list_positions(self) -> List[Any]:
        raise NotImplementedError


