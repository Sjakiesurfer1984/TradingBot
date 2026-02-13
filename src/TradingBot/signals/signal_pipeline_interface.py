from __future__ import annotations

from abc import ABC, abstractmethod

from TradingBot.domain.signals import SignalSnapshot
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC


class SignalPipelineABC(ABC):
    """
    Build per-cycle signals from an already-built snapshot.

    Contract
    - Snapshot IO (broker calls) happens before this pipeline runs.
    - Pipeline must be pure with respect to broker IO.
    - Pipeline returns an immutable SignalSnapshot for the cycle.
    """

    @abstractmethod
    def build(self, snapshot: CycleSnapshotABC) -> SignalSnapshot:
        raise NotImplementedError
