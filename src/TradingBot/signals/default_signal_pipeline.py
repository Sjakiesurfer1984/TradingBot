from __future__ import annotations

from TradingBot.domain.signals import SignalSnapshot
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.signals.signal_pipeline_interface import SignalPipelineABC


class DefaultSignalPipeline(SignalPipelineABC):
    """
    Phase 1 default pipeline.

    Behaviour
    - Produces baseline signals only.
    - Later you can swap this for a composite pipeline (GNN regime, IV predictor, etc).
    """

    def build(self, snapshot: CycleSnapshotABC) -> SignalSnapshot:
        return SignalSnapshot.empty(as_of_utc=snapshot.as_of_utc())
