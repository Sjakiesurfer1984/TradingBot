from __future__ import annotations

from dataclasses import dataclass

from typing import List
from uuid import uuid4

from src.domain.orders import (
    MarketOrder,
    MultiLegLimitOrder,
    OptionContract,
    OptionLeg,
    OptionRight,
    OrderABC,
    OrderSide,
    TimeInForce,
)
from src.execution.execution_policy_interface import ExecutionPolicyABC
from src.execution.order_specs import OptionMarketOrderSpec, PmccOrderSpec
from src.orchestration.cycle_snapshot import CycleSnapshotABC
from src.risk.decisions import ApprovedIntent
from src.risk.pmcc_sizer import parse_osi
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("DefaultExecutionPolicy")


@dataclass(frozen=True)
class DefaultExecutionPolicy(ExecutionPolicyABC):

    def to_orders(self, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        orders: List[OrderABC] = []

        for approval in approvals:
            spec = approval.approval_payload

            with log_scope("execution_policy.to_orders", logger, extra=type(spec).__name__):
                if isinstance(spec, PmccOrderSpec):
                    order = self._pmcc_to_mleg(spec)
                    orders.append(order)

                elif isinstance(spec, OptionMarketOrderSpec):
                    order = self._option_spec_to_market(spec)
                    orders.append(order)

                else:
                    logger.warning("No order builder for spec type=%s — skipping", type(spec).__name__)

        return orders

    # ------------------------------------------------------------------

    def _pmcc_to_mleg(self, spec: PmccOrderSpec) -> MultiLegLimitOrder:
        leap_parsed = parse_osi(spec.leap.option_symbol)
        near_parsed = parse_osi(spec.near.option_symbol)

        leap_contract = OptionContract(
            underlying=str(spec.underlying),
            expiry=leap_parsed.expiry,
            strike=leap_parsed.strike,
            right=OptionRight.CALL,
            option_symbol=spec.leap.option_symbol,
        )
        near_contract = OptionContract(
            underlying=str(spec.underlying),
            expiry=near_parsed.expiry,
            strike=near_parsed.strike,
            right=OptionRight.CALL,
            option_symbol=spec.near.option_symbol,
        )

        return MultiLegLimitOrder(
            client_order_id=f"pmcc-{uuid4().hex[:12]}",
            underlying=spec.underlying,
            legs=[
                OptionLeg(contract=leap_contract, side=OrderSide.BUY,  ratio=1),
                OptionLeg(contract=near_contract, side=OrderSide.SELL, ratio=1),
            ],
            quantity=spec.quantity,
            limit_price=spec.limit_price,
            time_in_force=spec.time_in_force,
        )

    def _option_spec_to_market(self, spec: OptionMarketOrderSpec) -> MarketOrder:
        intent = spec.position_intent.upper()
        side   = OrderSide.BUY if intent in ("BTO", "BTC") else OrderSide.SELL

        return MarketOrder(
            client_order_id=f"opt-{uuid4().hex[:12]}",
            symbol=spec.option_symbol,
            side=side,
            quantity=spec.quantity,
            time_in_force=spec.time_in_force,
        )
