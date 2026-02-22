from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from src.utilities.logger import setup_logger

logger = setup_logger("PmccStateClassifier")


class PmccState(str, Enum):
    FLAT      = "flat"
    LEAP_ONLY = "leap_only"
    COVERED   = "covered"
    NEAR_ONLY = "near_only"   # illegal — should not occur
    PENDING   = "pending"     # open order blocking action


@dataclass(frozen=True)
class PmccHoldings:
    state:         PmccState
    leap_position: Optional[Dict[str, Any]]
    near_position: Optional[Dict[str, Any]]


class PmccStateClassifier:
    """Derives current PMCC state from positions + open orders snapshots."""

    def classify(
        self,
        *,
        underlying:  str,
        positions:   List[Dict[str, Any]],
        open_orders: List[Dict[str, Any]],
    ) -> PmccHoldings:
        sym = underlying.strip().upper()

        # ------------------------------------------------------------------
        # Filter option positions for this underlying.
        # Alpaca returns asset_class="us_option" and underlying_symbol="SPY".
        # qty can be positive (long) or negative (short).
        # ------------------------------------------------------------------
        opt_pos = [
            p for p in positions
            if str(p.get("underlying_symbol", "")).upper() == sym
            and str(p.get("asset_class", "")).lower() == "us_option"
        ]

        logger.info(
            "Classifier | underlying=%s total_positions=%d option_positions=%d",
            sym, len(positions), len(opt_pos),
        )
        for p in opt_pos:
            logger.info(
                "  OptionPos | symbol=%s qty=%s side=%s asset_class=%s underlying=%s",
                p.get("symbol"), p.get("qty"), p.get("side"),
                p.get("asset_class"), p.get("underlying_symbol"),
            )

        # ------------------------------------------------------------------
        # Identify LEAP vs NEAR by DTE derived from the OSI symbol.
        # Alpaca does NOT include a "dte" field on positions — parse from symbol.
        # ------------------------------------------------------------------
        leap = next((p for p in opt_pos if self._is_leap(p)), None)
        near = next((p for p in opt_pos if self._is_near(p)), None)

        logger.info(
            "Classifier | leap=%s near=%s",
            leap.get("symbol") if leap else "none",
            near.get("symbol") if near else "none",
        )

        # ------------------------------------------------------------------
        # Check for pending orders that should block action.
        # MLEG orders have no top-level underlying_symbol — must check legs.
        # ------------------------------------------------------------------
        pending_orders = [o for o in open_orders if self._order_is_for(o, sym)]

        logger.info(
            "Classifier | open_orders_total=%d pending_for_%s=%d",
            len(open_orders), sym, len(pending_orders),
        )
        for o in pending_orders:
            logger.info(
                "  PendingOrder | id=%s class=%s status=%s symbol=%s",
                o.get("id"), o.get("order_class"), o.get("status"), o.get("symbol"),
            )

        # ------------------------------------------------------------------
        # Derive state
        # ------------------------------------------------------------------
        if pending_orders:
            state = PmccState.PENDING
        elif leap and near:
            state = PmccState.COVERED
        elif leap and not near:
            state = PmccState.LEAP_ONLY
        elif not leap and near:
            state = PmccState.NEAR_ONLY
        else:
            state = PmccState.FLAT

        logger.info("Classifier | result state=%s", state.value)
        return PmccHoldings(state=state, leap_position=leap, near_position=near)

    @staticmethod
    def _order_is_for(order: Dict[str, Any], sym: str) -> bool:
        """
        Return True if this open order relates to the given underlying.
        Handles both simple orders (symbol field) and MLEG orders (legs list).
        """
        # Simple order: top-level symbol or underlying_symbol matches
        top_sym = str(order.get("underlying_symbol") or order.get("symbol") or "").upper()
        if top_sym == sym:
            return True

        # MLEG order: check legs for any leg whose underlying matches.
        # Alpaca MLEG legs have a "symbol" field with the OSI option symbol,
        # e.g. "SPY270617C00605000" — the underlying is the alpha prefix.
        for leg in (order.get("legs") or []):
            if not isinstance(leg, dict):
                continue
            leg_sym = str(leg.get("symbol") or "").upper()
            # Extract underlying from OSI: leading alpha characters
            underlying_part = ""
            for ch in leg_sym:
                if ch.isalpha():
                    underlying_part += ch
                else:
                    break
            if underlying_part == sym:
                return True

        return False

    @staticmethod
    def _is_leap(position: Dict[str, Any]) -> bool:
        """
        A LEAP is a long option with DTE > 90 days.
        qty > 0 means long (bought).
        """
        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            return False  # short positions are never LEAPs in our PMCC

        return _dte_from_position(position) > 90

    @staticmethod
    def _is_near(position: Dict[str, Any]) -> bool:
        """
        A NEAR is a short option with DTE <= 90 days.
        qty < 0 means short (sold).
        """
        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty >= 0:
            return False  # long positions are never NEARs in our PMCC

        return _dte_from_position(position) <= 90


def _dte_from_position(position: Dict[str, Any]) -> int:
    """
    Derive DTE from the OSI option symbol on the position.
    Alpaca positions do not carry a dte field directly.
    """
    sym = str(position.get("symbol", ""))
    if len(sym) >= 15:
        try:
            from src.risk.pmcc_sizer import parse_osi
            parsed = parse_osi(sym)
            return max(0, (parsed.expiry - datetime.now()).days)
        except Exception as exc:
            logger.warning("Could not parse DTE from symbol=%s: %s", sym, exc)
    return -1