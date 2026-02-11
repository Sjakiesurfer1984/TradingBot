from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Sequence

from TradingBot.domain.orders import (
    MultiLegLimitOrder,
    OptionContract,
    OptionLeg,
    OptionRight,
    OrderABC,
    OrderSide,
)
from TradingBot.execution.execution_policy_interface import ExecutionPolicyABC
from TradingBot.execution.order_specs import PmccOrderSpec
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.risk.decisions import ApprovedIntent
from TradingBot.risk.pmcc_sizer import parse_osi
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("DefaultExecutionPolicy")


@dataclass(frozen=True)
class DefaultExecutionPolicy(ExecutionPolicyABC):
    """
    Default execution policy.

    Responsibilities
    - Convert policy-neutral specs into domain orders.
    - Keep broker coupling out of risk and strategy layers.
    """

    def to_orders(self, snapshot: CycleSnapshotABC, approvals: List[ApprovedIntent]) -> List[OrderABC]:
        orders: List[OrderABC] = []

        with log_scope("execution_policy.to_orders", logger, extra=f"approvals={len(approvals)}"):
            for approval in approvals:
                spec = approval.approval_payload

                if isinstance(spec, PmccOrderSpec):
                    orders.append(self._to_pmcc_order(approval=approval, spec=spec))
                    continue

                raise ValueError(f"Unsupported approval spec type: {type(spec).__name__}")

        return orders

    def _to_pmcc_order(self, *, approval: ApprovedIntent, spec: PmccOrderSpec) -> MultiLegLimitOrder:
        leap = parse_osi(spec.leap_contract_symbol)
        near = parse_osi(spec.near_contract_symbol)

        leap_contract = OptionContract(
            underlying=spec.underlying_symbol,
            expiry=leap.expiry_utc,
            strike=Decimal(str(leap.strike)),
            right=OptionRight.CALL,
            option_symbol=leap.option_symbol,
        )
        near_contract = OptionContract(
            underlying=spec.underlying_symbol,
            expiry=near.expiry_utc,
            strike=Decimal(str(near.strike)),
            right=OptionRight.CALL,
            option_symbol=near.option_symbol,
        )

        legs: Sequence[OptionLeg] = (
            OptionLeg(contract=leap_contract, side=OrderSide.BUY, ratio=1),
            OptionLeg(contract=near_contract, side=OrderSide.SELL, ratio=1),
        )

        return MultiLegLimitOrder(
            client_order_id=approval.client_order_id,
            underlying=spec.underlying_symbol,
            legs=legs,
            quantity=int(spec.quantity),
            limit_price=Decimal(str(spec.limit_price)).quantize(Decimal("0.0001")),
            time_in_force=spec.time_in_force,
        )
