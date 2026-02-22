from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class RegimeLabel(str, Enum):
    UNKNOWN = "unknown"
    BULL    = "bull"
    BEAR    = "bear"
    WHIPSAW = "whipsaw"


@dataclass(frozen=True)
class SignalSnapshot:
    as_of_utc:         datetime
    regime:            RegimeLabel         = RegimeLabel.UNKNOWN
    regime_confidence: float               = 0.0
    scalar_signals:    Mapping[str, float] = field(default_factory=dict)
    iv_forecast:       Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls, *, as_of_utc: datetime) -> "SignalSnapshot":
        return cls(as_of_utc=as_of_utc)
