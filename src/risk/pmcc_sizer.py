from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class OsiParsed:
    underlying: str
    expiry:     datetime
    right:      str   # "C" | "P"
    strike:     Decimal


_OSI_RE = re.compile(
    r"^(?P<sym>[A-Z]+)"
    r"(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<right>[CP])"
    r"(?P<strike>\d{8})$"
)


def parse_osi(symbol: str) -> OsiParsed:
    m = _OSI_RE.match(symbol.strip().upper())
    if not m:
        raise ValueError(f"Cannot parse OSI symbol: {symbol!r}")
    yy    = int(m["yy"])
    year  = 2000 + yy if yy < 50 else 1900 + yy
    expiry = datetime(year, int(m["mm"]), int(m["dd"]))
    strike = Decimal(m["strike"]) / Decimal("1000")
    return OsiParsed(
        underlying=m["sym"],
        expiry=expiry,
        right=m["right"],
        strike=strike,
    )


@dataclass(frozen=True)
class PmccSizingConfig:
    equity_budget_pct:        float = 0.05
    max_option_bp_fraction:   float = 0.25
    max_debit_per_spread_usd: float = 5000.0
    max_contracts_per_intent: int   = 5
    slippage_factor:          float = 1.02


@dataclass
class PmccSizer:
    config: PmccSizingConfig

    def size(
        self,
        *,
        equity:              float,
        option_buying_power: float,
        leap_ask:            float,
        near_bid:            float,
    ) -> Optional[int]:
        if leap_ask <= 0 or near_bid < 0:
            return None

        net_debit_per_contract = (leap_ask - near_bid) * 100
        if net_debit_per_contract <= 0:
            return None

        net_debit_with_slip = net_debit_per_contract * self.config.slippage_factor

        budget_equity = equity * self.config.equity_budget_pct
        budget_bp     = option_buying_power * self.config.max_option_bp_fraction
        budget        = min(budget_equity, budget_bp, self.config.max_debit_per_spread_usd)

        if budget <= 0:
            return None

        contracts = int(budget // net_debit_with_slip)
        contracts = min(contracts, self.config.max_contracts_per_intent)
        return contracts if contracts > 0 else None
