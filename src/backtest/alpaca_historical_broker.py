from __future__ import annotations

"""
src/backtest/alpaca_historical_broker.py
-----------------------------------------
Implements BrokerABC against Alpaca's historical data API and a
SimulatedAccount for order execution.

Data sources:
  - Stock quotes:   /v2/stocks/{sym}/bars  (daily OHLCV, use close as mid)
  - Option chains:  /v1beta1/options/snapshots/{sym}  (same endpoint as live,
                    but we pass a date parameter to get historical EOD data)

Fill model:
  - Market orders (BTC/STO single-leg): fill at bid (sells) or ask (buys)
    from the historical chain snapshot for that date.
  - MLEG limit orders (PMCC entry): fill at the limit price if
    (leap_ask - near_bid) <= limit_price, otherwise not filled this cycle.

Alpaca's historical options snapshot endpoint returns EOD data when you
pass `feed=indicative` and a specific date. We request one date at a time.

The broker is stateless w.r.t. dates — the BacktestRunner sets the current
date before each cycle via set_date().
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
from requests import Session
from requests.adapters import HTTPAdapter

from src.backtest.simulated_account import SimulatedAccount
from src.brokers.broker_base import BrokerBase
from src.brokers.errors import BrokerConnectionError
from src.brokers.interfaces import BrokerABC
from src.domain.orders import (
    MarketOrder,
    MultiLegLimitOrder,
    OrderABC,
    OrderSide as DomainOrderSide,
)
from src.domain.types import AssetQuote, Symbol
from src.utilities.logger import setup_logger

logger = setup_logger("AlpacaHistoricalBroker")

_STOCK_BASE  = "https://data.alpaca.markets/v2"
_OPTION_BASE = "https://data.alpaca.markets/v1beta1"


@dataclass
class AlpacaHistoricalBroker(BrokerBase, BrokerABC):
    """
    BrokerABC implementation for backtesting.

    The runner calls set_date() at the start of each cycle to point the
    broker at the correct historical date. All data fetches then use that
    date. Orders are simulated against the same day's prices.

    Rate limiting: Alpaca's data API allows ~200 req/min on free tier.
    We add a small sleep between chain fetches to stay safe.
    """

    account:       SimulatedAccount
    api_key:       str
    api_secret:    str
    rate_limit_sleep: float = 0.4   # seconds between API calls

    _current_date: date               = field(init=False, default_factory=date.today)
    _session:      Optional[Session]  = field(init=False, default=None)
    # Cache chain data within a single cycle — avoid re-fetching the same
    # chain multiple times (get_option_chain_requests may request leaps and
    # shorts separately but they often overlap).
    _chain_cache:  Dict[str, List[Dict[str, Any]]] = field(default_factory=dict, init=False)
    _bar_cache:    Dict[str, float]                 = field(default_factory=dict, init=False)

    # ------------------------------------------------------------------
    # Date control (called by BacktestRunner)
    # ------------------------------------------------------------------

    def set_date(self, d: date) -> None:
        self._current_date = d
        self._chain_cache.clear()
        self._bar_cache.clear()
        logger.info("BacktestBroker date set | date=%s", d.isoformat())

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _ensure_session(self) -> Session:
        if self._session is None:
            s = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=2)
            s.mount("https://", adapter)
            self._session = s
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    def _get(self, url: str, params: Optional[Dict] = None) -> Any:
        time.sleep(self.rate_limit_sleep)
        session  = self._ensure_session()
        response = session.get(url, headers=self._headers(), params=params, timeout=(5, 30))
        if response.status_code == 429:
            logger.warning("Rate limited — sleeping 60s")
            time.sleep(60)
            response = session.get(url, headers=self._headers(), params=params, timeout=(5, 30))
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # ExecutionBrokerABC
    # ------------------------------------------------------------------

    def get_account_snapshot(self) -> Dict[str, Any]:
        return {
            "equity":               str(self.account.equity),
            "options_buying_power": str(self.account.option_buying_power),
            "cash":                 str(self.account.cash),
        }

    def get_option_buying_power(self) -> float:
        return self.account.option_buying_power

    def get_equity(self) -> float:
        return self.account.equity

    def get_positions(self) -> List[Dict[str, Any]]:
        return self.account.positions()

    def get_open_orders(self) -> List[Dict[str, Any]]:
        # In backtest all orders fill same-day — no open orders carry over.
        return []

    def submit_order(self, order: OrderABC) -> Any:
        date_str = self._current_date.isoformat()

        if isinstance(order, MultiLegLimitOrder):
            return self._fill_mleg(order, date_str)

        if isinstance(order, MarketOrder):
            return self._fill_market(order, date_str)

        raise TypeError(f"Unsupported order type: {type(order).__name__}")

    def cancel_order(self, order_id: str) -> None:
        pass  # No open orders in backtest

    # ------------------------------------------------------------------
    # MarketDataProviderABC
    # ------------------------------------------------------------------

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        """
        Return EOD close price for the symbol on _current_date.
        We use the daily bar close as both bid and ask (no spread model
        for the underlying — we only need it for spot price in strategy logic).
        """
        sym   = symbol.strip().upper()
        price = self._get_bar_close(sym)
        return AssetQuote(
            symbol=sym,
            bid=price,
            ask=price,
            mid=price,
            timestamp_utc=datetime.combine(
                self._current_date, datetime.min.time(), tzinfo=timezone.utc
            ),
        )

    def get_latest_price(self, symbol: str) -> float:
        return self._get_bar_close(symbol.strip().upper())

    def get_daily_bars(self, symbol: str, lookback_days: int) -> Any:
        sym   = symbol.strip().upper()
        end   = self._current_date
        start = end - timedelta(days=lookback_days + 5)
        url   = f"{_STOCK_BASE}/stocks/{sym}/bars"
        return self._get(url, params={
            "timeframe":  "1Day",
            "start":      start.isoformat(),
            "end":        end.isoformat(),
            "adjustment": "raw",
            "feed":       "iex",
            "limit":      1000,
        })

    def get_option_chain(
        self,
        underlying: str,
        *,
        include_calls:       bool            = True,
        include_puts:        bool            = False,
        feed:                str             = "indicative",
        max_age_seconds:     int             = 30,
        limit:               int             = 0,
        strike_price_gte:    Optional[float] = None,
        strike_price_lte:    Optional[float] = None,
        expiration_date:     Optional[date]  = None,
        expiration_date_gte: Optional[date]  = None,
        expiration_date_lte: Optional[date]  = None,
        root_symbol:         Optional[str]   = None,
        updated_since:       Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        sym = underlying.strip().upper()
        url = f"{_OPTION_BASE}/options/snapshots/{sym}"

        params: Dict[str, Any] = {
            "feed": "indicative",
            # Historical EOD snapshot — pass the date so Alpaca returns
            # end-of-day data rather than live quotes.
            "date": self._current_date.isoformat(),
        }

        if include_calls and not include_puts:
            params["type"] = "call"
        elif include_puts and not include_calls:
            params["type"] = "put"
        if expiration_date is not None:
            params["expiration_date"] = expiration_date.isoformat()
        if expiration_date_gte is not None:
            params["expiration_date_gte"] = expiration_date_gte.isoformat()
        if expiration_date_lte is not None:
            params["expiration_date_lte"] = expiration_date_lte.isoformat()
        if strike_price_gte is not None:
            params["strike_price_gte"] = strike_price_gte
        if strike_price_lte is not None:
            params["strike_price_lte"] = strike_price_lte
        if root_symbol:
            params["root_symbol"] = root_symbol.strip().upper()

        # Cache key — same params within a cycle reuse the cached result.
        cache_key = f"{sym}:{self._current_date}:{params.get('type','')}:" \
                    f"{params.get('expiration_date_gte','')}:{params.get('expiration_date_lte','')}"
        if cache_key in self._chain_cache:
            logger.info("Chain cache hit | key=%s", cache_key)
            return self._chain_cache[cache_key]

        chain: List[Dict[str, Any]] = []
        next_page: Optional[str] = None

        while True:
            p = dict(params)
            if next_page:
                p["page_token"] = next_page
            data = self._get(url, params=p)
            if not isinstance(data, dict):
                raise BrokerConnectionError("alpaca-historical", f"Unexpected response for {sym}")
            for contract_sym, snap in (data.get("snapshots") or {}).items():
                if isinstance(snap, dict):
                    row = dict(snap)
                    row["contract_symbol"] = contract_sym
                    chain.append(row)
                    if 0 < limit <= len(chain):
                        self._chain_cache[cache_key] = chain
                        return chain
            nxt = data.get("next_page_token")
            next_page = str(nxt).strip() if isinstance(nxt, str) and nxt.strip() else None
            if not next_page:
                break

        logger.info(
            "Historical chain fetched | sym=%s date=%s contracts=%d",
            sym, self._current_date, len(chain),
        )
        self._chain_cache[cache_key] = chain
        return chain

    # ------------------------------------------------------------------
    # Fill simulation
    # ------------------------------------------------------------------

    def _fill_mleg(self, order: MultiLegLimitOrder, date_str: str) -> None:
        """
        Fill a PMCC entry (BTO LEAP + STO NEAR) at historical EOD prices.

        We accept the fill if the net debit implied by historical prices
        is <= the limit price on the order. This matches how a limit order
        would behave — if the market is worse than the limit, no fill.
        """
        underlying = str(order.underlying).upper()

        # Gather fill prices for each leg from the chain cache.
        leg_fills: List[Dict[str, Any]] = []
        for leg in order.legs:
            sym   = str(leg.contract.option_symbol).upper()
            price = self._get_option_price(sym, leg.side)
            if price is None:
                logger.warning(
                    "MLEG fill skipped — no price for %s on %s", sym, date_str,
                )
                return
            leg_fills.append({
                "symbol": sym, "side": leg.side, "price": price,
                "underlying": underlying,
            })

        # Net debit = buy legs - sell legs (per contract, pre-multiplier)
        net_debit = sum(
            f["price"] if f["side"] == DomainOrderSide.BUY else -f["price"]
            for f in leg_fills
        )

        limit = float(order.limit_price)
        if net_debit > limit * 1.02:   # 2% tolerance for EOD vs intraday
            logger.info(
                "MLEG fill skipped — net_debit %.2f > limit %.2f | date=%s",
                net_debit, limit, date_str,
            )
            return

        qty = int(order.quantity)
        for fill in leg_fills:
            if fill["side"] == DomainOrderSide.BUY:
                self.account.fill_buy(
                    symbol=fill["symbol"], underlying=fill["underlying"],
                    qty=qty, fill_price=fill["price"], date_str=date_str,
                )
            else:
                self.account.fill_sell(
                    symbol=fill["symbol"], underlying=fill["underlying"],
                    qty=qty, fill_price=fill["price"], date_str=date_str,
                )

    def _fill_market(self, order: MarketOrder, date_str: str) -> None:
        """
        Fill a single-leg market order (BTC or STO) at historical EOD prices.
        """
        sym        = str(order.symbol).upper()
        underlying = self._underlying_from_osi(sym)
        price      = self._get_option_price(sym, order.side)

        if price is None:
            logger.warning(
                "Market fill skipped — no price for %s on %s", sym, date_str,
            )
            return

        qty = int(order.quantity)
        if order.side == DomainOrderSide.BUY:
            self.account.fill_buy(
                symbol=sym, underlying=underlying,
                qty=qty, fill_price=price, date_str=date_str,
            )
        else:
            self.account.fill_sell(
                symbol=sym, underlying=underlying,
                qty=qty, fill_price=price, date_str=date_str,
            )

    # ------------------------------------------------------------------
    # Price lookup helpers
    # ------------------------------------------------------------------

    def _get_option_price(
        self,
        osi_symbol: str,
        side: DomainOrderSide,
    ) -> Optional[float]:
        """
        Find the fill price for an option from the cached chain data.
        Buys fill at ask; sells fill at bid. Falls back to mid if one
        side is missing.
        """
        for chain_data in self._chain_cache.values():
            for row in chain_data:
                if str(row.get("contract_symbol", "")).upper() == osi_symbol:
                    q   = row.get("latestQuote") or {}
                    ask = float(q.get("ap") or 0)
                    bid = float(q.get("bp") or 0)
                    mid = (ask + bid) / 2.0 if ask > 0 and bid > 0 else ask or bid
                    if side == DomainOrderSide.BUY:
                        return ask if ask > 0 else (mid if mid > 0 else None)
                    else:
                        return bid if bid > 0 else (mid if mid > 0 else None)

        # Not in any cached chain — fetch directly (rare: only for management
        # orders where chain wasn't fetched this cycle).
        logger.warning(
            "Option not in chain cache — fetching single snapshot | sym=%s", osi_symbol,
        )
        underlying = self._underlying_from_osi(osi_symbol)
        try:
            chain = self.get_option_chain(underlying)
            for row in chain:
                if str(row.get("contract_symbol", "")).upper() == osi_symbol:
                    q   = row.get("latestQuote") or {}
                    ask = float(q.get("ap") or 0)
                    bid = float(q.get("bp") or 0)
                    mid = (ask + bid) / 2.0 if ask > 0 and bid > 0 else ask or bid
                    if side == DomainOrderSide.BUY:
                        return ask if ask > 0 else (mid if mid > 0 else None)
                    else:
                        return bid if bid > 0 else (mid if mid > 0 else None)
        except Exception as e:
            logger.warning("Failed to fetch price for %s: %s", osi_symbol, e)
        return None

    def _get_bar_close(self, symbol: str) -> float:
        """Fetch EOD close for the underlying on _current_date."""
        if symbol in self._bar_cache:
            return self._bar_cache[symbol]

        # We request a small window around the target date to handle
        # weekends/holidays — take the last bar at or before our date.
        start = (self._current_date - timedelta(days=5)).isoformat()
        end   = self._current_date.isoformat()
        url   = f"{_STOCK_BASE}/stocks/{symbol}/bars"
        data  = self._get(url, params={
            "timeframe":  "1Day",
            "start":      start,
            "end":        end,
            "adjustment": "raw",
            "feed":       "iex",
            "limit":      10,
        })
        bars = data.get("bars") or []
        if not bars:
            raise BrokerConnectionError(
                "alpaca-historical",
                f"No bar data for {symbol} around {self._current_date}",
            )
        close = float(bars[-1]["c"])
        self._bar_cache[symbol] = close
        logger.info("Bar close | symbol=%s date=%s close=%.2f", symbol, self._current_date, close)
        return close

    def update_position_market_values(self) -> None:
        """
        Revalue all open positions from cached chain prices.
        Called by the runner at end of each cycle for accurate equity tracking.
        """
        prices: Dict[str, float] = {}
        for chain_data in self._chain_cache.values():
            for row in chain_data:
                sym = str(row.get("contract_symbol", "")).upper()
                if not sym:
                    continue
                q   = row.get("latestQuote") or {}
                ask = float(q.get("ap") or 0)
                bid = float(q.get("bp") or 0)
                mid = (ask + bid) / 2.0 if ask > 0 and bid > 0 else ask or bid
                if mid > 0:
                    prices[sym] = mid
        self.account.update_market_values(prices)

    @staticmethod
    def _underlying_from_osi(osi_symbol: str) -> str:
        ticker = []
        for ch in osi_symbol.strip().upper():
            if ch.isdigit():
                break
            ticker.append(ch)
        return "".join(ticker)
