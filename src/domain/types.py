from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import NewType, Optional

IntentId      = NewType("IntentId",      str)
StrategyId    = NewType("StrategyId",    str)
ClientOrderId = NewType("ClientOrderId", str)
Symbol        = NewType("Symbol",        str)


def normalise_symbol(value: str) -> Symbol:
    if not isinstance(value, str):
        raise TypeError("Symbol must be a str")
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("Symbol must not be empty")
    return Symbol(cleaned)


@dataclass(frozen=True, slots=True)
class AssetQuote:
    symbol:        str
    bid:           Optional[float]
    ask:           Optional[float]
    mid:           Optional[float]
    timestamp_utc: Optional[datetime]


@dataclass(frozen=True)
class OptionChainRequest:
    underlying:          str
    request_id:          str
    include_calls:       bool           = True
    include_puts:        bool           = False
    feed:                str            = "indicative"
    limit:               int            = 0
    strike_price_gte:    Optional[float]    = None
    strike_price_lte:    Optional[float]    = None
    expiration_date:     Optional[date]     = None
    expiration_date_gte: Optional[date]     = None
    expiration_date_lte: Optional[date]     = None
    root_symbol:         Optional[str]      = None
    updated_since:       Optional[datetime] = None
