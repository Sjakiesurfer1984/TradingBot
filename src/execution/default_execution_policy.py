from __future__ import annotations

"""
src/execution/default_execution_policy.py

Converts ApprovedIntents to broker orders and records fills in the DB.

SRP:  owns "translate an approved intent into broker effects".
OCP:  new strategy → new payload roles but same 3 types → zero changes here.
DIP:  depends on TradeDatabaseABC, never on TradeDatabase directly.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import uuid4

from src.domain.intents import (
    PositionIntent,
    RollPayload,
    SingleLegPayload,
    SpreadPayload,
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
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("DefaultExecutionPolicy")


def _coid() -> ClientOrderId:
    return ClientOrderId(str(uuid4())[:16])


def _parse_osi(symbol: str) -> Tuple[Optional[date], Optional[float], Optional[str]]:
    try:
        from src.risk.pmcc_sizer import parse_osi
        parsed = parse_osi(symbol)
        expiry = parsed.expiry
        if hasattr(expiry, "date"):
            expiry = expiry.date()
        return expiry, float(parsed.strike), parsed.right
    except Exception:
        return None, None, None


def _order_side(intent: PositionIntent) -> OrderSide:
    return OrderSide.BUY if intent in (PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE) \
        else OrderSide.SELL


@dataclass
class DefaultExecutionPolicy(ExecutionPolicyABC):
    """
    Translates ApprovedIntents into broker orders.

    Three payload types map to three builders:

      SpreadPayload    → ONE MultiLegLimitOrder
                         (limit_price and quantity set by evaluator)
      RollPayload      → TWO sequential MarketOrders
                         (MLEG rejects rolls — STO would be uncovered at submission)
      SingleLegPayload → ONE MarketOrder
    """

    db:      TradeDatabaseABC = field(default_factory=NullTradeDatabase)
    dry_run: bool             = False

    def to_orders(self, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        orders: List[OrderABC] = []
        for approval in approvals:
            with log_scope("execution_policy.to_orders", logger,
                           extra=type(approval.payload).__name__):
                orders.extend(self._build(approval))
        return orders

    def _build(self, approval: ApprovedIntent) -> List[OrderABC]:
        payload = approval.payload

        if isinstance(payload, SpreadPayload):
            return [self._build_spread(payload)]

        if isinstance(payload, RollPayload):
            return self._build_roll(payload)

        if isinstance(payload, SingleLegPayload):
            return [self._build_single_leg(payload)]

        logger.warning(
            "No order builder for payload type=%s — skipping",
            type(payload).__name__,
        )
        return []

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _build_spread(self, payload: SpreadPayload) -> MultiLegLimitOrder:
        """
        Two-leg atomic order.

        For entries: quantity and limit_price are set by the evaluator.
        For closes:  quantity defaults to 1, limit_price defaults to 0.00.
        """
        legs = [
            OptionLeg(
                contract=payload.leg_a.contract.contract,
                side=_order_side(payload.leg_a.position_intent),
                ratio=1,
            ),
            OptionLeg(
                contract=payload.leg_b.contract.contract,
                side=_order_side(payload.leg_b.position_intent),
                ratio=1,
            ),
        ]
        qty   = payload.quantity or 1
        price = payload.limit_price or 0.0

        order = MultiLegLimitOrder(
            client_order_id=_coid(),
            underlying=Symbol(str(payload.underlying_symbol)),
            legs=legs,
            quantity=qty,
            limit_price=Decimal(str(price)),
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Spread order | underlying=%s %s=%s %s=%s qty=%d limit=%.2f",
            payload.underlying_symbol,
            payload.leg_a.role, payload.leg_a.contract.option_symbol,
            payload.leg_b.role, payload.leg_b.contract.option_symbol,
            qty, price,
        )
        self._record(
            payload.leg_a.contract.option_symbol,
            payload.leg_a.position_intent.value,
            str(payload.underlying_symbol),
            qty,
            payload.leg_a.role,
        )
        self._record(
            payload.leg_b.contract.option_symbol,
            payload.leg_b.position_intent.value,
            str(payload.underlying_symbol),
            qty,
            payload.leg_b.role,
        )
        return order

    def _build_roll(self, payload: RollPayload) -> List[MarketOrder]:
        """Two sequential MarketOrders: close first, then open."""
        close_side = _order_side(payload.close.position_intent)
        open_side  = _order_side(payload.open_.position_intent)

        close_order = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.close.contract.option_symbol,
            side=close_side,
            quantity=1,
            time_in_force=TimeInForce.DAY,
        )
        open_order = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.open_.contract.option_symbol,
            side=open_side,
            quantity=1,
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Roll orders | close=%s open=%s (sequential — MLEG not supported for rolls)",
            payload.close.contract.option_symbol,
            payload.open_.contract.option_symbol,
        )
        self._record(
            payload.close.contract.option_symbol,
            payload.close.position_intent.value,
            str(payload.underlying_symbol),
            1,
            payload.close.role,
        )
        self._record(
            payload.open_.contract.option_symbol,
            payload.open_.position_intent.value,
            str(payload.underlying_symbol),
            1,
            payload.open_.role,
        )
        return [close_order, open_order]

    def _build_single_leg(self, payload: SingleLegPayload) -> MarketOrder:
        """One MarketOrder — any single-leg action."""
        side  = _order_side(payload.leg.position_intent)
        order = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.leg.contract.option_symbol,
            side=side,
            quantity=payload.qty,
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Single leg order | symbol=%s side=%s qty=%d role=%s intent=%s",
            payload.leg.contract.option_symbol,
            side.value,
            payload.qty,
            payload.leg.role,
            payload.leg.position_intent.value,
        )
        self._record(
            payload.leg.contract.option_symbol,
            payload.leg.position_intent.value,
            str(payload.underlying_symbol),
            payload.qty,
            payload.leg.role,
        )
        return order

    # ------------------------------------------------------------------
    # Fill recording
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