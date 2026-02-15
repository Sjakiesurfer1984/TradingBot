from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from TradingBot.domain.types import Symbol


class PmccState(str, Enum):
    FLAT = "flat"
    LEAP_ONLY = "leap_only"
    COVERED = "covered"
    NEAR_ONLY = "near_only"


@dataclass(frozen=True)
class PmccHeldLegs:
    underlying: Symbol
    held_leap_symbol: Optional[str]
    held_near_symbol: Optional[str]
    held_near_qty_abs: int
    state: PmccState


@dataclass(frozen=True)
class PmccStateClassifier:
    """
    Derive PMCC state from the snapshot.

    Contract
    - No broker IO.
    - Only inspects snapshot positions and option chains.
    """

    leap_request_id: str = "pmcc_leap"
    near_request_id: str = "pmcc_near"

    @staticmethod
    def _safe_str(v: Any) -> str:
        return "" if v is None else str(v)

    @staticmethod
    def _safe_float(v: Any) -> float:
        try:
            return float(v)
        except Exception:
            return 0.0

    def classify(
        self,
        *,
        underlying: Symbol,
        positions: List[Dict[str, Any]],
        leap_chain: List[Dict[str, Any]],
        near_chain: List[Dict[str, Any]],
    ) -> PmccHeldLegs:
        leap_symbols: Set[str] = {
            self._safe_str(r.get("contract_symbol")).strip().upper()
            for r in leap_chain
            if self._safe_str(r.get("contract_symbol")).strip()
        }
        near_symbols: Set[str] = {
            self._safe_str(r.get("contract_symbol")).strip().upper()
            for r in near_chain
            if self._safe_str(r.get("contract_symbol")).strip()
        }

        held_leap: Optional[str] = None
        held_near: Optional[str] = None
        held_near_qty_abs: int = 0

        for p in positions:
            if not isinstance(p, dict):
                continue

            sym: str = self._safe_str(p.get("symbol")).strip().upper()
            if not sym:
                continue

            qty_raw: Any = p.get("qty") if "qty" in p else p.get("quantity")
            qty: float = self._safe_float(qty_raw)

            if sym in leap_symbols and qty > 0:
                held_leap = sym

            if sym in near_symbols:
                if qty < 0:
                    held_near = sym
                    held_near_qty_abs = max(held_near_qty_abs, int(abs(qty)))
                elif qty > 0:
                    held_near = sym
                    held_near_qty_abs = max(held_near_qty_abs, int(abs(qty)))

        if held_leap is None and held_near is None:
            state = PmccState.FLAT
        elif held_leap is not None and held_near is None:
            state = PmccState.LEAP_ONLY
        elif held_leap is not None and held_near is not None:
            state = PmccState.COVERED
        else:
            state = PmccState.NEAR_ONLY

        return PmccHeldLegs(
            underlying=underlying,
            held_leap_symbol=held_leap,
            held_near_symbol=held_near,
            held_near_qty_abs=int(held_near_qty_abs) if held_near_qty_abs > 0 else 1,
            state=state,
        )
