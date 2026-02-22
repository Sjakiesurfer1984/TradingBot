from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Sequence

from src.domain.types import ClientOrderId, Symbol


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"


class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class OptionRight(str, Enum):
    CALL = "call"
    PUT  = "put"


class OrderABC(ABC):
    """Root type for all domain orders."""


@dataclass(frozen=True)
class OptionContract:
    underlying:    str
    expiry:        datetime
    strike:        Decimal
    right:         OptionRight
    option_symbol: str


@dataclass(frozen=True)
class OptionLeg:
    contract: OptionContract
    side:     OrderSide
    ratio:    int = 1


@dataclass(frozen=True)
class MultiLegLimitOrder(OrderABC):
    client_order_id: ClientOrderId
    underlying:      Symbol
    legs:            Sequence[OptionLeg]
    quantity:        int
    limit_price:     Decimal
    time_in_force:   TimeInForce = TimeInForce.DAY


@dataclass(frozen=True)
class MarketOrder(OrderABC):
    client_order_id: ClientOrderId
    symbol:          str
    side:            OrderSide
    quantity:        int
    time_in_force:   TimeInForce = TimeInForce.DAY
