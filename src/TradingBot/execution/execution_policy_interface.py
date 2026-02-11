from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from TradingBot.domain.orders import OrderABC
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.risk.decisions import ApprovedIntent


class ExecutionPolicyABC(ABC):
    """
    Execution policy contract (UML).

    Contract
    - Converts risk-approved intent specs into broker-agnostic domain orders (OrderABC).
    - Contains no broker IO.
    """

    @abstractmethod
    def to_orders(self, snapshot: CycleSnapshotABC, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        raise NotImplementedError
