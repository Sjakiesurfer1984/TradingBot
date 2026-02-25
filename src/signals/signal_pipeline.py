from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.signals import SignalSnapshot
from src.orchestration.cycle_snapshot import CycleSnapshot


class SignalPipelineABC(ABC):
    @abstractmethod
    def compute(self, snapshot: CycleSnapshot) -> SignalSnapshot:
        raise NotImplementedError


class DefaultSignalPipeline(SignalPipelineABC):
    """No-op pipeline — returns empty signals. Replace with real implementation."""

    def compute(self, snapshot: CycleSnapshot) -> SignalSnapshot:
        return SignalSnapshot.empty(as_of_utc=snapshot.as_of_utc)