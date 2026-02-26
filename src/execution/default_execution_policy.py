from __future__ import annotations

"""
src/execution/default_execution_policy.py

Converts ApprovedIntents to broker orders and records fills in the DB.

SRP:  this class owns "translate an approved intent into broker effects".
      Two outputs of the same action:
        Output 1 — OrderABC objects sent to the broker
        Output 2 — fill records written to the DB with leg_role
      Both are consequences of the same decision; they belong together.

OCP:  add a new payload type → add one _build_* method + one _record() call.
      Zero other changes required.

DIP:  depends on TradeDatabaseABC (abstraction), not TradeDatabase (concretion).
      Defaults to NullTradeDatabase so callers without persistence need not change.

Composition: db injected, never inherited.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import uuid4

from src.domain.intents import (
    CloseLegPayload,
    CloseSpreadPayload,
    RollNearPayload,
)
from src.domain.orders import (
    MarketOrder,
    MultiLegLimitOrder,
    OptionLeg,
    OrderABC,
    OrderSide,
    TimeInForce,
)
from src.domain.types import ClientOrderId, Symbol
from src.execution.execution_policy_interface import ExecutionPolicyABC
from src.persistence.interfaces import NullTradeDatabase, TradeDatabaseABC
from src.risk.decisions import ApprovedIntent
from src.risk.evaluators import _EnterPmccApproved
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("DefaultExecutionPolicy")


def _coid() -> ClientOrderId:
    return ClientOrderId(str(uuid4())[:16])


def _parse_osi(symbol: str) -> Tuple[Optional[date], Optional[float], Optional[str]]:
    """
    Extract (expiry, strike, right) from an OSI option symbol.
    Single point of OSI parsing — delegates to pmcc_sizer.parse_osi.
    Returns (None, None, None) on failure rather than raising.
    """
    try:
        from src.risk.pmcc_sizer import parse_osi
        parsed = parse_osi(symbol)
        expiry = parsed.expiry
        if hasattr(expiry, "date"):
            expiry = expiry.date()
        return expiry, float(parsed.strike), parsed.right
    except Exception:
        return None, None, None


@dataclass
class DefaultExecutionPolicy(ExecutionPolicyABC):
    """
    Converts ApprovedIntents to broker-domain orders.

    db is injected for fill recording. Each _build_* method records its
    own fills — one payload type, one method, one place to change (OCP).

    Alpaca MLEG constraints:
      EnterPmccPayload   → 1x MultiLegLimitOrder  (BTO leap + STO near, covered ✅)
      RollNearPayload    → 2x MarketOrder sequential (MLEG rejects rolls ❌)
      CloseLegPayload    → 1x MarketOrder
      CloseSpreadPayload → 1x MultiLegLimitOrder   (both legs closing ✅)
    """

    db:      TradeDatabaseABC = field(default_factory=NullTradeDatabase)
    dry_run: bool             = False  # when True, _record() is a no-op

    def to_orders(self, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        orders: List[OrderABC] = []
        for approval in approvals:
            with log_scope("execution_policy.to_orders", logger,
                           extra=type(approval.payload).__name__):
                orders.extend(self._build(approval))
        return orders

    def _build(self, approval: ApprovedIntent) -> List[OrderABC]:
        payload = approval.payload

        if isinstance(payload, _EnterPmccApproved):
            return [self._build_entry(payload)]
        if isinstance(payload, RollNearPayload):
            return self._build_roll(payload)
        if isinstance(payload, CloseLegPayload):
            return [self._build_close_leg(payload)]
        if isinstance(payload, CloseSpreadPayload):
            return [self._build_close_spread(payload)]

        logger.warning(
            "No order builder for payload type=%s — skipping",
            type(payload).__name__,
        )
        return []

    # ------------------------------------------------------------------
    # Builders — each builds its orders AND records its fills.
    # One payload type → one method → one place to change (OCP).
    # ------------------------------------------------------------------

    def _build_entry(self, payload: _EnterPmccApproved) -> MultiLegLimitOrder:
        """PMCC entry → one MultiLegLimitOrder (BTO leap + STO near)."""
        legs = [
            OptionLeg(contract=payload.leap.contract, side=OrderSide.BUY,  ratio=1),
            OptionLeg(contract=payload.near.contract, side=OrderSide.SELL, ratio=1),
        ]
        order = MultiLegLimitOrder(
            client_order_id=_coid(),
            underlying=Symbol(str(payload.underlying_symbol)),
            legs=legs,
            quantity=payload.quantity,
            limit_price=Decimal(str(payload.limit_price)),
            time_in_force=payload.time_in_force,
        )
        logger.info(
            "Entry order | underlying=%s leap=%s near=%s qty=%d debit=%.2f",
            payload.underlying_symbol,
            payload.leap.option_symbol,
            payload.near.option_symbol,
            payload.quantity,
            payload.limit_price,
        )
        self._record(payload.leap.option_symbol, "BTO",
                     str(payload.underlying_symbol), payload.quantity, "leap")
        self._record(payload.near.option_symbol, "STO",
                     str(payload.underlying_symbol), payload.quantity, "near")
        return order

    def _build_roll(self, payload: RollNearPayload) -> List[MarketOrder]:
        """NEAR roll → two sequential MarketOrders (BTC then STO)."""
        btc = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.close.option_symbol,
            side=OrderSide.BUY,
            quantity=1,
            time_in_force=TimeInForce.DAY,
        )
        sto = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.open.option_symbol,
            side=OrderSide.SELL,
            quantity=1,
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Roll orders | btc=%s sto=%s (sequential — MLEG not supported for rolls)",
            payload.close.option_symbol,
            payload.open.option_symbol,
        )
        self._record(payload.close.option_symbol, "BTC",
                     str(payload.underlying_symbol), 1, "near")
        self._record(payload.open.option_symbol,  "STO",
                     str(payload.underlying_symbol), 1, "near")
        return [btc, sto]

    def _build_close_leg(self, payload: CloseLegPayload) -> MarketOrder:
        """
        Single-leg order — covers both opening and closing actions:
          BTO → buy to open  (new long LEAP)
          STO → sell to open (new short NEAR)
          BTC → buy to close (close short NEAR)
          STC → sell to close (close long LEAP)
        """
        intent = payload.position_intent.value
        side   = OrderSide.BUY if intent in ("BTC", "BTO") else OrderSide.SELL
        order  = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.contract.option_symbol,
            side=side,
            quantity=payload.qty,
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Single leg order | symbol=%s side=%s qty=%d intent=%s",
            payload.contract.option_symbol, side.value, payload.qty, intent,
        )
        # Role: BTC/STO = near leg, BTO/STC = leap leg
        role = "near" if intent in ("BTC", "STO") else "leap"
        self._record(payload.contract.option_symbol, intent,
                     str(payload.underlying_symbol), payload.qty, role)
        return order

    def _build_close_spread(self, payload: CloseSpreadPayload) -> MultiLegLimitOrder:
        """Close entire spread → one MultiLegLimitOrder (BTC near + STC leap)."""
        legs = [
            OptionLeg(contract=payload.near.contract, side=OrderSide.BUY,  ratio=1),
            OptionLeg(contract=payload.leap.contract, side=OrderSide.SELL, ratio=1),
        ]
        order = MultiLegLimitOrder(
            client_order_id=_coid(),
            underlying=Symbol(str(payload.underlying_symbol)),
            legs=legs,
            quantity=1,
            limit_price=Decimal("0.00"),
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Close spread order | underlying=%s near=%s leap=%s",
            payload.underlying_symbol,
            payload.near.option_symbol,
            payload.leap.option_symbol,
        )
        self._record(payload.near.option_symbol, "BTC",
                     str(payload.underlying_symbol), 1, "near")
        self._record(payload.leap.option_symbol, "STC",
                     str(payload.underlying_symbol), 1, "leap")
        return order

    # ------------------------------------------------------------------
    # Fill recording — private, called only from _build_* methods.
    # Fill price is unknown at submission; recorded as 0.0 for now.
    # ------------------------------------------------------------------

    def _record(
        self,
        symbol:     str,
        action:     str,
        underlying: str,
        qty:        int,
        leg_role:   str,
    ) -> None:
        if self.dry_run:
            logger.debug(
                "DRY RUN — fill not recorded | symbol=%s action=%s role=%s",
                symbol, action, leg_role,
            )
            return
        expiry, strike, right = _parse_osi(symbol)
        try:
            self.db.record_fill(
                action=action,
                symbol=symbol,
                underlying=underlying,
                qty=qty,
                fill_price=0.0,
                fill_date=date.today(),
                expiry=expiry,
                strike=strike,
                option_right=right,
                cost_basis_usd=0.0,
                leg_role=leg_role,
                notes="recorded at submission — fill price TBD",
            )
        except Exception:
            logger.exception(
                "Fill recording failed | symbol=%s action=%s role=%s",
                symbol, action, leg_role,
            )