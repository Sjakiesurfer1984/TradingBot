from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from TradingBot.domain.signals import SignalSnapshot
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.signals.signal_pipeline_interface import SignalPipelineABC


@dataclass(frozen=True)
class PmccSignalPipeline(SignalPipelineABC):
    """
    Phase 1 PMCC signal plumbing.

    Behaviour
    - Produces a SignalSnapshot with placeholder scalars only.
    - Later you can replace this with real RSI and IV measures.
    """

    def build(self, snapshot: CycleSnapshotABC) -> SignalSnapshot:
        scalars: Dict[str, float] = {}

        # These keys are stable integration points for the strategy.
        # They can be filled by a real indicator engine later.
        scalars["pmcc.iv_rank"] = 0.0
        scalars["pmcc.rsi"] = 0.0

        return SignalSnapshot(
            as_of_utc=snapshot.as_of_utc(),
            scalar_signals=scalars,
        )
