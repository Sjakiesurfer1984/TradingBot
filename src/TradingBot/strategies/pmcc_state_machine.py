from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class PmccState(str, Enum):
    FLAT = "flat"
    LEAP_ONLY = "leap_only"
    PMCC_OPEN = "pmcc_open"
    NEAR_ONLY = "near_only"
    PENDING = "pending"


@dataclass(frozen=True)
class PmccLegSnapshot:
    symbol: str
    qty: int


@dataclass(frozen=True)
class PmccSnapshot:
    underlying: str
    leap: Optional[PmccLegSnapshot]
    near: Optional[PmccLegSnapshot]
    has_blocking_open_orders: bool


def derive_pmcc_state(s: PmccSnapshot) -> PmccState:
    if s.has_blocking_open_orders:
        return PmccState.PENDING

    if s.leap is None and s.near is None:
        return PmccState.FLAT

    if s.leap is not None and s.near is None:
        return PmccState.LEAP_ONLY

    if s.leap is None and s.near is not None:
        return PmccState.NEAR_ONLY

    return PmccState.PMCC_OPEN


def has_blocking_open_orders(
    *,
    underlying: str,
    open_orders: Iterable[object],
) -> bool:
    """
    Minimal, broker-agnostic filter.
    Treat any open option order on the same underlying as blocking.
    """
    for o in open_orders:
        symbol = getattr(o, "symbol", None)
        if not symbol:
            continue

        if str(symbol).startswith(str(underlying)):
            return True

    return False
