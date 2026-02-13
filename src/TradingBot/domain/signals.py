from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Mapping


class RegimeLabel(str, Enum):
    """
    Market regime label.

    Notes
    - This is deliberately minimal in Phase 1.
    - Later, our GNN regime determiner can output richer labels and metadata.
    """

    UNKNOWN = "unknown"
    BULL = "bull"
    BEAR = "bear"
    WHIPSAW = "whipsaw"


@dataclass(frozen=True)
class SignalSnapshot:
    """
    Immutable per-cycle signal bundle.

    Contract
    - Signals are computed once per cycle.
    - Strategies read signals from the snapshot.
    - Risk and execution do not need signals in Phase 1.
    """

    as_of_utc: datetime
    regime: RegimeLabel = RegimeLabel.UNKNOWN
    regime_confidence: float = 0.0

    # Placeholders for Phase 1 wiring.
    # Later we can extend without changing CycleSnapshot shape.
    scalar_signals: Mapping[str, float] = field(default_factory=dict)
    iv_forecast: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls, *, as_of_utc: datetime) -> "SignalSnapshot":
        """
        Create a baseline signal snapshot.

        Why this exists
        - Phase 1 is about plumbing, not signal quality.
        - Returning an explicit object avoids None checks throughout the codebase.
        """
        return cls(as_of_utc=as_of_utc)
