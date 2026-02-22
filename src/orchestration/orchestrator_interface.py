from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CycleRunResult:
    orders_submitted: int
    intents_generated: int
    intents_approved: int
    intents_rejected: int


class OrchestratorABC(ABC):
    @abstractmethod
    def run_cycle(self) -> CycleRunResult:
        raise NotImplementedError
