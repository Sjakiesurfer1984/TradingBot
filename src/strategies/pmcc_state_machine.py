from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from src.utilities.logger import setup_logger

logger = setup_logger("PmccStateClassifier")


class PmccState(str, Enum):
    FLAT      = "flat"       # no positions at all
    LEAP_ONLY = "leap_only"  # long LEAP contracts > short NEAR contracts
    COVERED   = "covered"    # long contracts == short contracts (fully paired)
    NEAR_ONLY = "near_only"  # short NEAR with no LEAP — illegal, must close
    PENDING   = "pending"    # a completing MLEG entry order is in-flight


@dataclass(frozen=True)
class PmccHoldings:
    state:           PmccState
    leap_positions:  List[Dict[str, Any]]  # enriched LEAP position dicts
    near_positions:  List[Dict[str, Any]]  # enriched NEAR position dicts
    long_contracts:  int                   # total long LEAP contracts (sum of abs(qty))
    short_contracts: int                   # total short NEAR contracts (sum of abs(qty))
    uncovered:       int                   # long_contracts - short_contracts

    @property
    def leap_position(self) -> Optional[Dict[str, Any]]:
        return self.leap_positions[0] if self.leap_positions else None

    @property
    def near_position(self) -> Optional[Dict[str, Any]]:
        return self.near_positions[0] if self.near_positions else None


# ---------------------------------------------------------------------------
# Single source of truth: underlying and DTE from OSI symbol
# ---------------------------------------------------------------------------

def _underlying_from_osi(osi_symbol: str) -> str:
    """
    Underlying ticker = all chars before the first digit in the OSI symbol.

    OSI format: <TICKER><YYMMDD><C|P><8-digit-strike>
      SPY270617C00605000  → SPY
      AAPL260121C00150000 → AAPL

    Never use 'all alpha chars' — the C/P right char is also alphabetic
    and would give 'SPYC' instead of 'SPY'.

    This is the ONLY place the underlying is derived from a position.
    Alpaca never populates underlying_symbol on option positions.
    """
    ticker = []
    for ch in osi_symbol.strip().upper():
        if ch.isdigit():
            break
        ticker.append(ch)
    return "".join(ticker)


def _dte_from_osi(osi_symbol: str) -> int:
    """
    DTE = days until expiry, derived from the OSI symbol date portion.

    This is the ONLY place DTE is derived for a position.
    Alpaca does not return a dte field on position objects.
    Returns -1 if the symbol cannot be parsed.
    """
    sym = str(osi_symbol).strip()
    if len(sym) >= 15:
        try:
            from src.risk.pmcc_sizer import parse_osi
            parsed = parse_osi(sym)
            return max(0, (parsed.expiry - datetime.now()).days)
        except Exception as exc:
            logger.warning("Could not parse DTE from symbol=%s: %s", sym, exc)
    return -1


def _abs_qty(position: Dict[str, Any]) -> int:
    """Number of contracts in a position, always positive."""
    try:
        return abs(int(float(position.get("qty") or 0)))
    except (TypeError, ValueError):
        return 0


def _enrich_position(position: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of the position dict with two derived fields added:
      underlying  — ticker from OSI (always a string, never None)
      derived_dte — days to expiry from OSI (int, -1 if unparseable)

    Every downstream caller reads these fields. No one re-derives from
    the raw OSI symbol outside this function.
    """
    osi = str(position.get("symbol", ""))
    return {
        **position,
        "underlying":  _underlying_from_osi(osi),
        "derived_dte": _dte_from_osi(osi),
    }


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@dataclass
class PmccStateClassifier:
    """
    Derives PMCC state from positions and open orders.

    Key design decisions:
    - DTE boundaries come from config, never hardcoded here.
    - Underlying is always derived from the OSI symbol (_underlying_from_osi).
    - Contract counts use abs(qty), not position count.
      A single position with qty=2 means 2 long contracts.
      2 long contracts vs 1 short contract → LEAP_ONLY (1 uncovered).

    State transitions:
      long > short  → LEAP_ONLY  (uncovered = long - short)
      long == short → COVERED    (fully paired, nothing to sell)
      long == 0, short > 0 → NEAR_ONLY (illegal — close the short)
      long == 0, short == 0 → FLAT (no positions)
      completing MLEG order exists → PENDING (wait for fill)
    """
    leap_dte_min: int   # from config: leap.dte_min  e.g. 365
    near_dte_max: int   # from config: short.dte_max e.g. 45

    def classify(
        self,
        *,
        underlying:  str,
        positions:   List[Dict[str, Any]],
        open_orders: List[Dict[str, Any]],
    ) -> PmccHoldings:
        sym = underlying.strip().upper()

        enriched = [_enrich_position(p) for p in positions]

        # Log every position with the derived fields — not the raw None fields
        for p in enriched:
            logger.info(
                "RawPosition | symbol=%s asset_class=%r qty=%s side=%s "
                "underlying=%s dte=%d",
                p.get("symbol"), p.get("asset_class"),
                p.get("qty"), p.get("side"),
                p["underlying"], p["derived_dte"],
            )

        opt_pos = [
            p for p in enriched
            if p["underlying"] == sym
            and str(p.get("asset_class", "")).lower() == "us_option"
        ]

        leaps = [p for p in opt_pos if self._is_leap(p)]
        nears = [p for p in opt_pos if self._is_near(p)]

        # Count contracts (qty), not positions.
        # qty=2 on one LEAP position = 2 long contracts, not 1.
        long_contracts  = sum(_abs_qty(p) for p in leaps)
        short_contracts = sum(_abs_qty(p) for p in nears)
        uncovered       = max(0, long_contracts - short_contracts)

        logger.info(
            "Classifier | underlying=%s total_positions=%d option_positions=%d "
            "leap_positions=%d near_positions=%d "
            "long_contracts=%d short_contracts=%d uncovered=%d "
            "leap_dte_min=%d near_dte_max=%d",
            sym, len(enriched), len(opt_pos),
            len(leaps), len(nears),
            long_contracts, short_contracts, uncovered,
            self.leap_dte_min, self.near_dte_max,
        )
        for p in opt_pos:
            logger.info(
                "  OptionPos | symbol=%s qty=%s dte=%d is_leap=%s is_near=%s",
                p.get("symbol"), p.get("qty"), p["derived_dte"],
                self._is_leap(p), self._is_near(p),
            )

        all_pending    = [o for o in open_orders if self._order_is_for(o, sym)]
        completing     = [o for o in all_pending if self._is_completing_mleg(o)]
        non_completing = [o for o in all_pending if not self._is_completing_mleg(o)]

        logger.info(
            "Classifier | open_orders_total=%d pending_for_%s=%d "
            "completing=%d non_completing=%d",
            len(open_orders), sym, len(all_pending),
            len(completing), len(non_completing),
        )
        for o in all_pending:
            legs_info = [
                f"{leg.get('symbol')}:{leg.get('side')}"
                for leg in (o.get("legs") or [])
                if isinstance(leg, dict)
            ]
            logger.info(
                "  PendingOrder | id=%s class=%s status=%s completing=%s legs=%s",
                o.get("id"), o.get("order_class"), o.get("status"),
                self._is_completing_mleg(o), legs_info or "n/a",
            )
        if non_completing:
            logger.warning(
                "Non-completing pending orders — ignoring | underlying=%s ids=%s",
                sym, [o.get("id") for o in non_completing],
            )

        if completing:
            state = PmccState.PENDING
        elif long_contracts > short_contracts:
            state = PmccState.LEAP_ONLY
        elif long_contracts > 0 and long_contracts == short_contracts:
            state = PmccState.COVERED
        elif long_contracts == 0 and short_contracts > 0:
            state = PmccState.NEAR_ONLY
        else:
            state = PmccState.FLAT

        logger.info(
            "Classifier | result state=%s long_contracts=%d short_contracts=%d uncovered=%d",
            state.value, long_contracts, short_contracts, uncovered,
        )

        return PmccHoldings(
            state=state,
            leap_positions=leaps,
            near_positions=nears,
            long_contracts=long_contracts,
            short_contracts=short_contracts,
            uncovered=uncovered,
        )

    def _is_leap(self, position: Dict[str, Any]) -> bool:
        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            return False
        return position.get("derived_dte", -1) >= self.leap_dte_min

    def _is_near(self, position: Dict[str, Any]) -> bool:
        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty >= 0:
            return False
        dte = position.get("derived_dte", -1)
        return 0 <= dte <= self.near_dte_max

    @staticmethod
    def _order_is_for(order: Dict[str, Any], sym: str) -> bool:
        top_sym = str(
            order.get("underlying_symbol") or order.get("symbol") or ""
        ).upper()
        if top_sym == sym:
            return True
        for leg in (order.get("legs") or []):
            if not isinstance(leg, dict):
                continue
            if _underlying_from_osi(str(leg.get("symbol") or "").upper()) == sym:
                return True
        return False

    @staticmethod
    def _is_completing_mleg(order: Dict[str, Any]) -> bool:
        if str(order.get("order_class", "")).lower() != "mleg":
            return False
        legs  = [l for l in (order.get("legs") or []) if isinstance(l, dict)]
        buys  = [l for l in legs if str(l.get("side", "")).lower() == "buy"]
        sells = [l for l in legs if str(l.get("side", "")).lower() == "sell"]
        return len(buys) == 1 and len(sells) == 1