from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.domain.intents import SelectedOption
from src.domain.orders import TimeInForce
from src.domain.types import Symbol


@dataclass(frozen=True)
class PmccOrderSpec:
    underlying:    Symbol
    leap:          SelectedOption
    near:          SelectedOption
    quantity:      int
    limit_price:   Decimal
    time_in_force: TimeInForce = TimeInForce.DAY


@dataclass(frozen=True)
class OptionMarketOrderSpec:
    underlying:       Symbol
    option_symbol:    str
    quantity:         int
    position_intent:  str          # "BTO" | "BTC" | "STO" | "STC"
    time_in_force:    TimeInForce  = TimeInForce.DAY
    limit_price:      Optional[Decimal] = None
