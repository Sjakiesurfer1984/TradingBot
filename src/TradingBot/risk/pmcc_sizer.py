# src/TradingBot/v2/pmcc_sizer.py
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional


@dataclass(frozen=True)
class PmccSizingConfig:
    equity_budget_pct: float
    max_option_bp_fraction: float
    max_debit_per_spread_usd: float
    max_contracts_per_intent: int
    slippage_factor: float
    max_leap_spread_pct: float
    max_near_spread_pct: float

    # Freshness policy (broker-agnostic)
    max_age_seconds_opra: int = 10
    max_age_seconds_indicative: int = 3 * 24 * 60 * 60  # 3 days
    # Existing mode flag, passed down from main
    ignore_spread_checks: bool = False

@dataclass(frozen=True)
class ParsedOsiOption:
    """
    Parsed OCC/OSI option symbol.

    The OSI format is:
    - root (1 to 6 letters, often padded to 6 in some venues, but not always)
    - YYMMDD
    - C or P
    - strike as 8 digits, where value is strike * 1000

    Example
    - SPY270319C00605000 -> root=SPY, expiry=2027-03-19, right=C, strike=605.000
    """
    root: str
    expiry_utc: datetime
    right: str
    strike: Decimal
    option_symbol: str


_OSI_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

def parse_osi(option_symbol: str) -> ParsedOsiOption:
    """
    Parse a variable-length OSI symbol safely using regex.

    This intentionally does not assume a fixed-length root.
    """
    sym: str = str(option_symbol).strip().upper()
    m = _OSI_RE.match(sym)
    if not m:
        raise ValueError(f"Invalid OSI option symbol: {option_symbol}")

    root, yy, mm, dd, cp, strike8 = m.groups()

    year: int = 2000 + int(yy)
    month: int = int(mm)
    day: int = int(dd)

    expiry_utc: datetime = datetime(year, month, day, tzinfo=timezone.utc)

    strike_int: int = int(strike8)
    strike: Decimal = (Decimal(strike_int) / Decimal("1000")).quantize(Decimal("0.001"))

    return ParsedOsiOption(
        root=root,
        expiry_utc=expiry_utc,
        right="call" if cp == "C" else "put",
        strike=strike,
        option_symbol=sym,
    )

@dataclass(frozen=True)
class PmccSizingResult:
    qty: int
    limit_price: float
    debit_per_spread_usd: float
    budget_dollars: float

    # Added: informational metadata for logs / audit
    feed: str
    chain_newest_ts_utc: Optional[datetime]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(now_utc: datetime, newest_ts_utc: datetime) -> float:
    return float((now_utc - newest_ts_utc).total_seconds())


def _freshness_threshold_seconds(cfg: PmccSizingConfig, feed: str) -> int:
    f: str = str(feed).strip().lower()
    if f == "opra":
        return int(cfg.max_age_seconds_opra)
    return int(cfg.max_age_seconds_indicative)

@dataclass(frozen=True)
class PmccSizer:
    cfg: PmccSizingConfig

    def size(
        self,
        *,
        equity: float,
        option_buying_power: float,
        leap: Any,
        near: Any,
    ) -> PmccSizingResult:
        """
        Compute quantity and limit price for a PMCC spread.

        Notes on freshness
        - This is broker-agnostic.
        - Strategy passes (feed, chain_newest_ts_utc) through SelectedOption.
        - Sizer enforces different max-age thresholds depending on feed type.
        """
        leap_ask: float = float(leap.ask_price)
        leap_bid: float = float(leap.bid_price)
        near_ask: float = float(near.ask_price)
        near_bid: float = float(near.bid_price)

        if leap_ask <= 0.0 or near_ask <= 0.0:
            raise ValueError("Ask prices must be positive for PMCC sizing")
        if leap_bid < 0.0 or near_bid < 0.0:
            raise ValueError("Bid prices must be non-negative for PMCC sizing")
        if leap_ask < leap_bid or near_ask < near_bid:
            raise ValueError("Ask must be >= bid for PMCC sizing")

        # Freshness check (feed-aware)
        now_utc: datetime = _now_utc()

        leap_feed: str = getattr(leap, "feed", "indicative")
        near_feed: str = getattr(near, "feed", "indicative")
        leap_newest: Optional[datetime] = getattr(leap, "chain_newest_ts_utc", None)
        near_newest: Optional[datetime] = getattr(near, "chain_newest_ts_utc", None)

        if leap_newest is not None:
            age: float = _age_seconds(now_utc, leap_newest)
            threshold: int = _freshness_threshold_seconds(self.cfg, leap_feed)
            if age > float(threshold):
                raise ValueError(
                    f"LEAP chain too old for feed={leap_feed} "
                    f"(age_seconds={age:.1f} > threshold={threshold})"
                )

        if near_newest is not None:
            age = _age_seconds(now_utc, near_newest)
            threshold = _freshness_threshold_seconds(self.cfg, near_feed)
            if age > float(threshold):
                raise ValueError(
                    f"NEAR chain too old for feed={near_feed} "
                    f"(age_seconds={age:.1f} > threshold={threshold})"
                )

        # Spread percentage checks
        leap_spread_pct: float = (leap_ask - leap_bid) / leap_ask
        near_spread_pct: float = (near_ask - near_bid) / near_ask

        if not bool(self.cfg.ignore_spread_checks):
            if leap_spread_pct > self.cfg.max_leap_spread_pct:
                raise ValueError(
                    f"LEAP spread too wide ({leap_spread_pct:.4f}); max allowed is {self.cfg.max_leap_spread_pct:.4f}"
                )
            if near_spread_pct > self.cfg.max_near_spread_pct:
                raise ValueError(
                    f"NEAR spread too wide ({near_spread_pct:.4f}); max allowed is {self.cfg.max_near_spread_pct:.4f}"
                )

        debit: float = leap_ask - near_bid
        if debit <= 0.0:
            raise ValueError(f"Non-positive net debit: {debit:.4f}")

        debit_per_spread_usd: float = debit * 100.0
        if debit_per_spread_usd > self.cfg.max_debit_per_spread_usd:
            raise ValueError(
                f"Debit per spread {debit_per_spread_usd:.2f} exceeds maximum {self.cfg.max_debit_per_spread_usd:.2f}"
            )

        budget_equity: float = float(equity) * self.cfg.equity_budget_pct
        budget_bp: float = float(option_buying_power) * self.cfg.max_option_bp_fraction
        budget_dollars: float = min(budget_equity, budget_bp)
        if budget_dollars <= 0.0:
            raise ValueError("Insufficient budget for PMCC (zero budget)")

        debit_buffered: float = debit_per_spread_usd * self.cfg.slippage_factor
        qty_raw: int = math.floor(budget_dollars / debit_buffered) if debit_buffered > 0.0 else 0
        qty: int = min(qty_raw, self.cfg.max_contracts_per_intent)
        if qty < 1:
            raise ValueError("Insufficient budget for even 1 PMCC contract after slippage and caps")

        limit_price: float = debit * self.cfg.slippage_factor

        newest_ts: Optional[datetime] = None
        if leap_newest and near_newest:
            newest_ts = max(leap_newest, near_newest)
        elif leap_newest:
            newest_ts = leap_newest
        elif near_newest:
            newest_ts = near_newest

        feed: str = leap_feed if leap_feed == near_feed else f"{leap_feed}+{near_feed}"

        return PmccSizingResult(
            qty=qty,
            limit_price=limit_price,
            debit_per_spread_usd=debit_per_spread_usd,
            budget_dollars=budget_dollars,
            feed=feed,
            chain_newest_ts_utc=newest_ts,
        )