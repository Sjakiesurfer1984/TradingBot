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
    yy     = int(m["yy"])
    year   = 2000 + yy if yy < 50 else 1900 + yy
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
    """
    All fields are required — no defaults.

    These must be supplied from config.yaml via _build_risk_engine() in main.py.
    If any are missing, the program will exit at startup with a clear message
    rather than silently trading with wrong parameters.

    Required config.yaml fields under strategies[*].pmcc.risk:
        max_buying_power_fraction  (float, e.g. 0.50)
        max_debit_per_spread_usd   (float, e.g. 14000.0)
        max_contracts_per_intent   (int,   e.g. 1)
        slippage_factor            (float, e.g. 1.05)
    """
    # Maximum fraction of available buying power to use per PMCC entry.
    # e.g. 0.50 = never spend more than 50% of buying power on one spread.
    max_buying_power_fraction: float

    # Hard dollar ceiling on the net debit per spread.
    # Whichever is lower — bp fraction or this cap — wins.
    max_debit_per_spread_usd: float

    # Hard contract ceiling per intent — absolute safety cap applied last.
    max_contracts_per_intent: int

    # Multiply estimated cost by this before checking budgets.
    # 1.05 = assume fills will be 5% worse than mid price.
    slippage_factor: float


@dataclass
class PmccSizer:
    config: PmccSizingConfig

    def size(
        self,
        *,
        buying_power: float,
        leap_ask:     float,
        near_bid:     float,
    ) -> Optional[int]:
        """
        Return the number of contracts to trade, or None if the trade is not viable.

        Sizing logic (in order):
          1. Compute net debit per contract (leap ask - near bid) × 100.
          2. Apply slippage buffer to be conservative on fill cost.
          3. Compute bp budget = buying_power × max_buying_power_fraction.
          4. Cap at max_debit_per_spread_usd (hard dollar ceiling).
          5. Divide budget by slippage-adjusted cost → max contracts.
          6. Apply max_contracts_per_intent as a final hard cap.

        Sizing is based purely on buying power, not equity. Sizing off equity
        creates a feedback loop where losses elsewhere shrink the budget here,
        potentially triggering forced liquidation of the spread mid-cycle.
        """
        if leap_ask <= 0 or near_bid < 0:
            return None

        net_debit_per_contract = (leap_ask - near_bid) * 100
        if net_debit_per_contract <= 0:
            return None

        cost_with_slip = net_debit_per_contract * self.config.slippage_factor
        bp_budget      = buying_power * self.config.max_buying_power_fraction
        budget         = min(bp_budget, self.config.max_debit_per_spread_usd)

        if budget <= 0:
            return None

        contracts = int(budget // cost_with_slip)
        contracts = min(contracts, self.config.max_contracts_per_intent)
        return contracts if contracts > 0 else None