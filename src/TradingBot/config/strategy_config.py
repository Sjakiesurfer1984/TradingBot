from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class PmccConfig:
    underlying_symbol: str
    max_units: int
    leap_dte_min: int
    leap_dte_max: int
    leap_target_delta: float
    short_dte_min: int
    short_dte_max: int
    short_target_delta: float


StrategyName = Literal["pmcc"]


@dataclass(frozen=True)
class StrategySpec:
    name: StrategyName
    pmcc: Optional[PmccConfig] = None
