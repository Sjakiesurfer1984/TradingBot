# """
# pmcc_sizer.py
# ================

# This module encapsulates the sizing logic for a Poor Man's Covered Call (PMCC)
# strategy.  It converts high-level intent selections (selected option objects
# with bid/ask prices) into a concrete quantity and limit price for a
# multi-leg order.  The sizing respects risk budgets (both equity and option
# buying power), enforces conservative pricing assumptions, and validates
# market quality via spread checks and maximum debit limits.

# The design separates configuration (``PmccSizingConfig``) from execution
# (``PmccSizer``) to make the risk layer testable and to centralise all
# PMCC-specific parameters.  A helper ``parse_opra`` is provided to convert
# broker option symbols (OPRA/OSI format) into domain ``OptionContract``
# instances, which are required by the order model.

# The intent of this module is *not* to submit orders directly—those tasks
# remain the responsibility of the risk engine and orchestrator.  The sizer
# simply tells you how many spread units you can afford and what your limit
# price should be.

# Note on OPRA subscription:
# -------------------------
# The ``parse_opra`` function included here does not require any external
# subscription.  It merely parses the 21-character OPRA/OSI string commonly
# used by brokers to identify option contracts.  This function will work with
# option symbols provided by the broker even if you do not have an OPRA market
# data subscription.
# """

# from __future__ import annotations

# from dataclasses import dataclass
# from datetime import datetime
# from decimal import Decimal
# import math
# from typing import Any

# from TradingBot.v2.domain.types import Symbol, normalise_symbol
# from TradingBot.v2.domain.orders import OptionContract, OptionRight
# import re

# @dataclass(frozen=True)
# class PmccSizingConfig:
#     """Configuration parameters for PMCC sizing.

#     All fractions should be expressed as decimals (e.g. 0.02 means 2%).
#     Monetary amounts are in USD.  These values are typically loaded from
#     environment variables by the main entry point (=main() ).
#     """

#     equity_budget_pct: float
#     max_option_bp_fraction: float
#     max_debit_per_spread_usd: float
#     max_contracts_per_intent: int
#     slippage_factor: float
#     max_leap_spread_pct: float
#     max_near_spread_pct: float


# @dataclass(frozen=True)
# class PmccSizingResult:
#     """Result of sizing a PMCC spread.

#     Attributes
#     ----------
#     qty:
#         Number of spread units to trade.
#     limit_price:
#         Per-unit debit to submit for the multi-leg order (not multiplied by 100).
#     debit_per_spread_usd:
#         Net debit of one spread in dollars (ask minus bid times 100), prior to
#         slippage adjustment.
#     budget_dollars:
#         The effective dollar budget used (minimum of equity and option BP caps).
#     """

#     qty: int
#     limit_price: float
#     debit_per_spread_usd: float
#     budget_dollars: float


# def parse_opra(option_symbol: str) -> OptionContract:
#     """Parse a broker option symbol (OSI/OPRA format) into an OptionContract.

#     Modern option symbols often omit the space-padding used in the legacy 2-character
#     OSI format. Instead of assuming fixed offsets, this function uses a regular
#     expression to split the symbol into its components:

#     - ``([A-Z]+)``: the underlying root/symbol (one or more uppercase letters)
#     - ``(\\d{6})``: a six-digit expiry date in YYMMDD format
#     - ``([CP])``: a single character indicating call (C) or put (P)
#     - ``(\\d{8})``: an eight-digit strike price scaled by 1/1000 (e.g. ``00097500`` -> 9.75)
#     - e.g. ``AAPL240119C00150000`` for an AAPL Jan 19 2024 Call at 150.00 strike
#     """

#     s: str = option_symbol.strip().upper()
#     # Match variable-length underlying + 6-digit expiry + call/put + 8-digit strike
#     m = re.fullmatch(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", s)
#     if not m:
#         raise ValueError(f"Invalid OSI option symbol: {option_symbol}")
#     underlying_raw, expiry_str, right_char, strike_str = m.groups()
#     underlying: str = underlying_raw.strip()

#     # Expiry (YYMMDD) -> datetime
#     try:
#         yy: int = int(expiry_str[0:2])
#         mm: int = int(expiry_str[2:4])
#         dd: int = int(expiry_str[4:6])
#     except ValueError:
#         raise ValueError(f"Invalid expiry in option symbol: {option_symbol}")

#     year: int = 2000 + yy
#     expiry: datetime = datetime(year, mm, dd)

#     # Right (call/put)
#     if right_char == 'C':
#         right: OptionRight = OptionRight.CALL
#     elif right_char == 'P':
#         right = OptionRight.PUT
#     else:
#         raise ValueError(f"Invalid option right in symbol: {option_symbol}")

#     # Strike (8 digits, scaled by 1000)
#     try:
#         strike_int: int = int(strike_str)
#     except ValueError:
#         raise ValueError(f"Invalid strike in option symbol: {option_symbol}")

#     strike: Decimal = Decimal(strike_int) / Decimal(1000)
#     return OptionContract(
#         underlying=normalise_symbol(underlying),
#         expiry=expiry,
#         strike=strike,
#         right=right,
#         option_symbol=option_symbol,
#     )

# class PmccSizer:
#     """Compute sizing for a PMCC given capital constraints and selected legs.

#     Use ``PmccSizer.size`` to determine how many spreads you can afford and what
#     limit price to submit.  Raise ``ValueError`` with a human-readable
#     explanation if sizing is not possible (e.g. budgets exhausted, spreads too
#     wide, or negative debit).

#     Parameters
#     ----------
#     cfg: PmccSizingConfig
#         Sizing parameters loaded from environment or config.  See
#         ``PmccSizingConfig`` for details.
#     """

#     def __init__(self, cfg: PmccSizingConfig) -> None:
#         self.cfg: PmccSizingConfig = cfg

#     def size(
#         self,
#         *,
#         equity: float,
#         option_buying_power: float,
#         leap: Any,
#         near: Any,
#     ) -> PmccSizingResult:
#         """Compute quantity and limit price for a PMCC spread.

#         Feed-aware spread filtering
#         - Indicative feeds are delayed/noisier and can show slightly wider spreads.
#         - OPRA (or real-time) is treated as the strict baseline.
#         """

#         # ----------------------------
#         # Helpers: detect feed
#         # ----------------------------
#         def _normalise_feed(feed_any: Any) -> str:
#             if feed_any is None:
#                 return "unknown"
#             s: str = str(feed_any).strip().lower()
#             if not s:
#                 return "unknown"
#             return s

#         def _get_feed(opt: Any) -> str:
#             """
#             Try a few common attribute names.
#             - SelectedOption might carry feed directly.
#             - Or it might carry a raw snapshot dict with "_feed" or "feed".
#             """
#             for attr in ("feed", "_feed", "data_feed", "quote_feed"):
#                 if hasattr(opt, attr):
#                     return _normalise_feed(getattr(opt, attr))

#             raw: Any = getattr(opt, "raw", None)
#             if isinstance(raw, dict):
#                 for key in ("_feed", "feed", "data_feed", "quote_feed"):
#                     if key in raw:
#                         return _normalise_feed(raw.get(key))

#             snap: Any = getattr(opt, "snapshot", None)
#             if isinstance(snap, dict):
#                 for key in ("_feed", "feed", "data_feed", "quote_feed"):
#                     if key in snap:
#                         return _normalise_feed(snap.get(key))

#             return "unknown"

#         leap_feed: str = _get_feed(leap)
#         near_feed: str = _get_feed(near)

#         # ----------------------------
#         # Spread thresholds by feed
#         # ----------------------------
#         # Defaults (suggested):
#         # - TBOT_PMCC_INDICATIVE_SPREAD_MULT=1.25  (or 1.50 if your indicative feed is messy)
#         # - TBOT_PMCC_UNKNOWN_FEED_SPREAD_MULT=1.00 (treat unknown as strict)
#         indicative_mult: float = float(getattr(self.cfg, "indicative_spread_mult", 1.25))
#         unknown_mult: float = float(getattr(self.cfg, "unknown_feed_spread_mult", 1.00))

#         def _mult_for(feed: str) -> float:
#             if feed == "indicative":
#                 return indicative_mult
#             if feed in ("opra", "realtime", "real_time", "sip"):  # treat these as strict baseline
#                 return 1.0
#             return unknown_mult

#         leap_max_spread_pct: float = float(self.cfg.max_leap_spread_pct) * _mult_for(leap_feed)
#         near_max_spread_pct: float = float(self.cfg.max_near_spread_pct) * _mult_for(near_feed)

#         # ----------------------------
#         # Extract quotes
#         # ----------------------------
#         leap_ask: float = float(leap.ask_price)
#         leap_bid: float = float(leap.bid_price)
#         near_ask: float = float(near.ask_price)
#         near_bid: float = float(near.bid_price)

#         # Validate quotes
#         if leap_ask <= 0.0 or near_ask <= 0.0:
#             raise ValueError("Ask prices must be positive for PMCC sizing")
#         if leap_bid < 0.0 or near_bid < 0.0:
#             raise ValueError("Bid prices must be non-negative for PMCC sizing")

#         # Spread percentages
#         leap_spread_pct: float = (leap_ask - leap_bid) / leap_ask if leap_ask > 0.0 else float("inf")
#         near_spread_pct: float = (near_ask - near_bid) / near_ask if near_ask > 0.0 else float("inf")

#         if leap_spread_pct > leap_max_spread_pct:
#             raise ValueError(
#                 "LEAP spread too wide "
#                 f"({leap_spread_pct:.4f}); max allowed is {leap_max_spread_pct:.4f} "
#                 f"(base={self.cfg.max_leap_spread_pct:.4f} feed={leap_feed})"
#             )

#         if near_spread_pct > near_max_spread_pct:
#             raise ValueError(
#                 "NEAR spread too wide "
#                 f"({near_spread_pct:.4f}); max allowed is {near_max_spread_pct:.4f} "
#                 f"(base={self.cfg.max_near_spread_pct:.4f} feed={near_feed})"
#             )

#         # Net debit per spread (raw, before slippage)
#         debit: float = leap_ask - near_bid
#         if debit <= 0.0:
#             raise ValueError(f"Non-positive net debit: {debit:.4f}")

#         debit_per_spread_usd: float = debit * 100.0
#         if debit_per_spread_usd > self.cfg.max_debit_per_spread_usd:
#             raise ValueError(
#                 f"Debit per spread {debit_per_spread_usd:.2f} exceeds maximum {self.cfg.max_debit_per_spread_usd:.2f}"
#             )

#         # Effective budget (equity vs option BP)
#         budget_equity: float = float(equity) * self.cfg.equity_budget_pct
#         budget_bp: float = float(option_buying_power) * self.cfg.max_option_bp_fraction
#         budget_dollars: float = min(budget_equity, budget_bp)

#         if budget_dollars <= 0.0:
#             raise ValueError("Insufficient budget for PMCC (zero budget)")

#         # Adjust debit for slippage
#         debit_buffered: float = debit_per_spread_usd * self.cfg.slippage_factor

#         # Compute maximum affordable quantity (integer contracts)
#         qty_raw: int = math.floor(budget_dollars / debit_buffered) if debit_buffered > 0.0 else 0
#         qty: int = min(qty_raw, self.cfg.max_contracts_per_intent)

#         if qty < 1:
#             raise ValueError("Insufficient budget for even 1 PMCC contract after slippage and caps")

#         # Limit price per spread (debit) after slippage; not multiplied by 100
#         limit_price: float = debit * self.cfg.slippage_factor

#         return PmccSizingResult(
#             qty=qty,
#             limit_price=limit_price,
#             debit_per_spread_usd=debit_per_spread_usd,
#             budget_dollars=budget_dollars,
#         )

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