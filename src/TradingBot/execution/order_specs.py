from __future__ import annotations

from dataclasses import dataclass

from TradingBot.domain.orders import TimeInForce
from TradingBot.domain.types import Symbol


@dataclass(frozen=True)
class PmccOrderSpec:
    """
    Policy-neutral spec for a PMCC spread.

    This is not an OrderABC.
    ExecutionPolicy converts this spec into a MultiLegLimitOrder.
    """

    underlying_symbol: Symbol
    leap_contract_symbol: str
    near_contract_symbol: str
    quantity: int
    limit_price: float
    time_in_force: TimeInForce
