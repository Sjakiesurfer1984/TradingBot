from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from src.domain.orders import OrderABC
from src.risk.decisions import ApprovedIntent


class ExecutionPolicyABC(ABC):
    @abstractmethod
    def to_orders(self, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        raise NotImplementedError
