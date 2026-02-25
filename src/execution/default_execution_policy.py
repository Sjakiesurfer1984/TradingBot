from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List
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
from src.risk.decisions import ApprovedIntent
from src.risk.evaluators import _EnterPmccApproved
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("DefaultExecutionPolicy")


def _coid() -> ClientOrderId:
    return ClientOrderId(str(uuid4())[:16])


@dataclass
class DefaultExecutionPolicy(ExecutionPolicyABC):
    """
    Converts ApprovedIntents to broker-domain orders.

    No isinstance ladder — each payload type is handled in its own method.
    Adding a new payload type = add one elif + one _build_* method.

    Alpaca MLEG constraints respected:
      EnterPmccPayload   → 1x MultiLegLimitOrder  (BTO leap + STO near — covered ✅)
      RollNearPayload    → 2x MarketOrder sequential (MLEG rejects rolls — uncovered ❌)
      CloseLegPayload    → 1x MarketOrder
      CloseSpreadPayload → 1x MultiLegLimitOrder   (both legs closing — no new short ✅)
    """

    def to_orders(self, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        orders: List[OrderABC] = []
        for approval in approvals:
            with log_scope("execution_policy.to_orders", logger, extra=type(approval.payload).__name__):
                new_orders = self._build(approval)
                orders.extend(new_orders)
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
    # Builders
    # ------------------------------------------------------------------

    def _build_entry(self, payload: _EnterPmccApproved) -> MultiLegLimitOrder:
        """
        PMCC entry → one MultiLegLimitOrder.

        BTO leap + STO near submitted as a single MLEG spread.
        Alpaca fills both legs atomically — no partial-fill risk on entry.
        """
        from src.domain.orders import OptionLeg
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
        return order

    def _build_roll(self, payload: RollNearPayload) -> List[MarketOrder]:
        """
        NEAR roll → two sequential MarketOrders.

        Alpaca Level 3 rejects a roll as a single MLEG: the STO leg would be
        uncovered at submission time (the BTC hasn't filled yet). Sequential
        single-leg orders are the only valid path.

        Order: BTC first (eliminates the short), then STO (opens new short,
        now covered by the existing LEAP).
        """
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
        return [btc, sto]

    def _build_close_leg(self, payload: CloseLegPayload) -> MarketOrder:
        """Single-leg close — BTC a short or STC a long."""
        side = (
            OrderSide.BUY
            if payload.position_intent.value in ("BTC", "BTO")
            else OrderSide.SELL
        )
        order = MarketOrder(
            client_order_id=_coid(),
            symbol=payload.contract.option_symbol,
            side=side,
            quantity=payload.qty,
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Close leg order | symbol=%s side=%s qty=%d intent=%s",
            payload.contract.option_symbol,
            side.value,
            payload.qty,
            payload.position_intent.value,
        )
        return order

    def _build_close_spread(self, payload: CloseSpreadPayload) -> MultiLegLimitOrder:
        """
        Close entire spread → one MultiLegLimitOrder.

        Both legs are closing positions — no uncovered short is created,
        so Alpaca MLEG accepts this.
        """
        legs = [
            OptionLeg(contract=payload.near.contract, side=OrderSide.BUY,  ratio=1),  # BTC near
            OptionLeg(contract=payload.leap.contract, side=OrderSide.SELL, ratio=1),  # STC leap
        ]
        order = MultiLegLimitOrder(
            client_order_id=_coid(),
            underlying=Symbol(str(payload.underlying_symbol)),
            legs=legs,
            quantity=1,
            limit_price=Decimal("0.00"),   # market-ish — caller can override
            time_in_force=TimeInForce.DAY,
        )
        logger.info(
            "Close spread order | underlying=%s near=%s leap=%s",
            payload.underlying_symbol,
            payload.near.option_symbol,
            payload.leap.option_symbol,
        )
        return order