from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from TradingBot.domain.types import Symbol


class PmccState(str, Enum):
    FLAT = "FLAT"
    LEAP_ONLY = "LEAP_ONLY"
    NEAR_ONLY = "NEAR_ONLY"
    COVERED = "COVERED"


@dataclass(frozen=True)
class PmccHoldings:
    state: PmccState
    underlying: Symbol
    held_leap_symbol: Optional[str]
    held_near_symbol: Optional[str]
    held_leap_qty_abs: int
    held_near_qty_abs: int


def _to_int_abs(v: Any) -> int:
    try:
        return int(abs(float(v)))
    except Exception:
        return 0


def _extract_position_qty_abs(pos: Dict[str, Any]) -> int:
    for key in ("qty", "quantity", "qty_available", "qty_owned", "position_qty"):
        if key in pos:
            qty = _to_int_abs(pos.get(key))
            if qty > 0:
                return qty
    return 0


def _chain_symbols(chain: List[Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for row in chain:
        sym_any: Any = row.get("contract_symbol")
        if isinstance(sym_any, str):
            sym: str = sym_any.strip().upper()
            if sym:
                out.add(sym)
    return out


class PmccStateClassifier:
    """
    Derives PMCC holdings state from positions + the option chains you requested for this cycle.

    It classifies by intersecting held position symbols with:
    - LEAP chain symbols (pmcc_leap request_id)
    - NEAR chain symbols (pmcc_near request_id)
    """

    def classify(
        self,
        *,
        underlying: Symbol,
        positions: List[Dict[str, Any]],
        leap_chain: List[Dict[str, Any]],
        near_chain: List[Dict[str, Any]],
    ) -> PmccHoldings:
        leap_syms: Set[str] = _chain_symbols(leap_chain)
        near_syms: Set[str] = _chain_symbols(near_chain)

        held_leap: Optional[str] = None
        held_near: Optional[str] = None
        leap_qty_abs: int = 0
        near_qty_abs: int = 0

        for p in positions:
            sym_any: Any = p.get("symbol")
            if not isinstance(sym_any, str):
                continue
            sym: str = sym_any.strip().upper()
            if not sym:
                continue

            if sym in leap_syms:
                held_leap = sym
                leap_qty_abs = max(leap_qty_abs, _extract_position_qty_abs(p))

            if sym in near_syms:
                held_near = sym
                near_qty_abs = max(near_qty_abs, _extract_position_qty_abs(p))

        if held_leap and held_near:
            state = PmccState.COVERED
        elif held_leap and not held_near:
            state = PmccState.LEAP_ONLY
        elif held_near and not held_leap:
            state = PmccState.NEAR_ONLY
        else:
            state = PmccState.FLAT

        return PmccHoldings(
            state=state,
            underlying=underlying,
            held_leap_symbol=held_leap,
            held_near_symbol=held_near,
            held_leap_qty_abs=int(leap_qty_abs),
            held_near_qty_abs=int(near_qty_abs),
        )
