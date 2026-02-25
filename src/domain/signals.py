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


class IvRegime(str, Enum):
    """
    Volatility regime derived from IV Rank (IVR).

    HIGH   → IVR > 50  — IV is elevated relative to its 52-week range.
                          Premium is rich. Exit shorts early (50% profit)
                          before vol compresses and theta advantage evaporates.

    NORMAL → IVR 25-50 — IV is in a normal range. Standard management.
                          Roll at 55% profit.

    LOW    → IVR < 25  — IV is suppressed. Premium is thin. Stay in longer
                          to capture more decay before rolling (65% profit).

    UNKNOWN → IVR could not be computed (insufficient history, API error).
               Fall back to the static config value.
    """
    HIGH    = "high"
    NORMAL  = "normal"
    LOW     = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IvRegimeSignal:
    """
    IV regime computed once per cycle by the signal pipeline.

    Fields:
      regime         — HIGH / NORMAL / LOW / UNKNOWN
      ivr            — IV Rank 0-100 (None if unavailable)
      iv_current     — current 30-day implied vol (e.g. 0.18 = 18%)
      iv_52w_high    — highest IV seen in the past 252 trading days
      iv_52w_low     — lowest IV seen in the past 252 trading days
      hv30           — 30-day historical (realised) vol
      iv_hv_ratio    — iv_current / hv30 (>1 means options are expensive)
    """
    regime:      IvRegime
    ivr:         float | None = None
    iv_current:  float | None = None
    iv_52w_high: float | None = None
    iv_52w_low:  float | None = None
    hv30:        float | None = None
    iv_hv_ratio: float | None = None

    @classmethod
    def unknown(cls) -> "IvRegimeSignal":
        return cls(regime=IvRegime.UNKNOWN)

    @classmethod
    def from_ivr(
        cls,
        *,
        ivr:         float,
        iv_current:  float,
        iv_52w_high: float,
        iv_52w_low:  float,
        hv30:        float | None = None,
    ) -> "IvRegimeSignal":
        if ivr > 50:
            regime = IvRegime.HIGH
        elif ivr < 25:
            regime = IvRegime.LOW
        else:
            regime = IvRegime.NORMAL

        iv_hv_ratio = (iv_current / hv30) if hv30 and hv30 > 0 else None

        return cls(
            regime=regime,
            ivr=round(ivr, 1),
            iv_current=round(iv_current, 4),
            iv_52w_high=round(iv_52w_high, 4),
            iv_52w_low=round(iv_52w_low, 4),
            hv30=round(hv30, 4) if hv30 is not None else None,
            iv_hv_ratio=round(iv_hv_ratio, 2) if iv_hv_ratio is not None else None,
        )


@dataclass(frozen=True)
class SignalSnapshot:
    as_of_utc:         datetime
    regime:            RegimeLabel    = RegimeLabel.UNKNOWN
    regime_confidence: float          = 0.0
    iv_regime:         IvRegimeSignal = field(default_factory=IvRegimeSignal.unknown)
    scalar_signals:    Mapping[str, float] = field(default_factory=dict)
    iv_forecast:       Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls, *, as_of_utc: datetime) -> "SignalSnapshot":
        return cls(as_of_utc=as_of_utc)