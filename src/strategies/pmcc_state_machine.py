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
    PENDING   = "pending"     # completing open order blocking action


@dataclass(frozen=True)
class PmccHoldings:
    state:         PmccState
    leap_position: Optional[Dict[str, Any]]
    near_position: Optional[Dict[str, Any]]


def _underlying_from_osi(osi_symbol: str) -> str:
    """Extract the underlying ticker from an OSI option symbol (e.g. SPY260306C00715000 → SPY)."""
    return "".join(c for c in osi_symbol if c.isalpha()).upper()


def _underlying_from_position(position: Dict[str, Any]) -> str:
    """
    Return the underlying ticker for a position.
    Alpaca sometimes omits underlying_symbol on option positions — fall back to
    parsing the leading alpha characters from the OSI option symbol.
    """
    sym = position.get("underlying_symbol") or ""
    if sym:
        return str(sym).strip().upper()
    osi = str(position.get("symbol", ""))
    return _underlying_from_osi(osi)


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
        # Alpaca returns asset_class="us_option". underlying_symbol can be None
        # on some responses — fall back to parsing the OSI symbol.
        # qty > 0 = long, qty < 0 = short.
        # ------------------------------------------------------------------
        opt_pos = [
            p for p in positions
            if _underlying_from_position(p) == sym
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
                p.get("asset_class"), _underlying_from_position(p),
            )

        # ------------------------------------------------------------------
        # Identify LEAP vs NEAR by DTE derived from the OSI symbol.
        # ------------------------------------------------------------------
        leap = next((p for p in opt_pos if self._is_leap(p)), None)
        near = next((p for p in opt_pos if self._is_near(p)), None)

        logger.info(
            "Classifier | leap=%s near=%s",
            leap.get("symbol") if leap else "none",
            near.get("symbol") if near else "none",
        )

        # ------------------------------------------------------------------
        # Pending orders: only block the cycle if there is a *completing* MLEG
        # open order — i.e. one buy leg + one sell leg for this underlying,
        # which would bring us to a fully covered state once filled.
        #
        # Non-completing orders (stale, wrong side count, etc.) are logged as
        # warnings but do NOT block action.
        # ------------------------------------------------------------------
        all_pending   = [o for o in open_orders if self._order_is_for(o, sym)]
        completing    = [o for o in all_pending if self._is_completing_mleg(o)]
        non_completing = [o for o in all_pending if not self._is_completing_mleg(o)]

        logger.info(
            "Classifier | open_orders_total=%d pending_for_%s=%d completing=%d non_completing=%d",
            len(open_orders), sym, len(all_pending), len(completing), len(non_completing),
        )
        for o in all_pending:
            legs_info = [
                f"{leg.get('symbol')}:{leg.get('side')}"
                for leg in (o.get("legs") or [])
                if isinstance(leg, dict)
            ]
            is_comp = self._is_completing_mleg(o)
            logger.info(
                "  PendingOrder | id=%s class=%s status=%s completing=%s legs=%s",
                o.get("id"), o.get("order_class"), o.get("status"), is_comp, legs_info or "n/a",
            )

        if non_completing:
            logger.warning(
                "Non-completing pending orders found — ignoring, will not block cycle | "
                "underlying=%s count=%d ids=%s",
                sym, len(non_completing), [o.get("id") for o in non_completing],
            )

        # ------------------------------------------------------------------
        # Derive state
        # ------------------------------------------------------------------
        if completing:
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _order_is_for(order: Dict[str, Any], sym: str) -> bool:
        """
        Return True if this open order relates to the given underlying.
        Handles both simple orders (top-level symbol) and MLEG orders (legs list).
        """
        top_sym = str(order.get("underlying_symbol") or order.get("symbol") or "").upper()
        if top_sym == sym:
            return True

        # MLEG: check legs — each leg carries the full OSI symbol
        for leg in (order.get("legs") or []):
            if not isinstance(leg, dict):
                continue
            leg_sym = str(leg.get("symbol") or "").upper()
            if _underlying_from_osi(leg_sym) == sym:
                return True

        return False

    @staticmethod
    def _is_completing_mleg(order: Dict[str, Any]) -> bool:
        """
        True if this pending order is a standard PMCC entry MLEG:
        exactly one buy leg (the LEAP) and one sell leg (the NEAR).
        When filled it would bring us to a fully covered state.
        """
        if str(order.get("order_class", "")).lower() != "mleg":
            return False
        legs = [leg for leg in (order.get("legs") or []) if isinstance(leg, dict)]
        buys  = [l for l in legs if str(l.get("side", "")).lower() == "buy"]
        sells = [l for l in legs if str(l.get("side", "")).lower() == "sell"]
        return len(buys) == 1 and len(sells) == 1

    @staticmethod
    def _is_leap(position: Dict[str, Any]) -> bool:
        """A LEAP is a long option (qty > 0) with DTE > 90 days."""
        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            return False
        return _dte_from_position(position) > 90

    @staticmethod
    def _is_near(position: Dict[str, Any]) -> bool:
        """A NEAR is a short option (qty < 0) with DTE <= 90 days."""
        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty >= 0:
            return False
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