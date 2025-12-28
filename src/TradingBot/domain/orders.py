from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"


class OrderClass(str, Enum):
    SIMPLE = "SIMPLE"
    MLEG = "MLEG"


@dataclass(frozen=True)
class OptionLeg:
    """
    A broker agnostic representation of a single option leg.

    This is intentionally small and explicit so that:
    - strategies can be tested without a broker SDK
    - broker adaptors can translate it to native SDK requests
    """
    symbol: str
    side: OrderSide
    ratio_qty: int


@dataclass(frozen=True)
class MultiLegLimitOrder:
    """
    A broker agnostic multi leg limit order.

    Notes:
    - qty represents the overall contract quantity for the order.
    - legs carry the per leg ratio quantities.
    - limit_price is the net debit or credit per spread, depending on broker conventions.

    For now you can keep limit_price as 0.0 if you are only testing wiring.
    """
    qty: int
    legs: List[OptionLeg]
    time_in_force: TimeInForce = TimeInForce.DAY
    order_class: OrderClass = OrderClass.MLEG
    limit_price: float = 0.0
    client_order_id: Optional[str] = None
