from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
from src.domain.types import AssetQuote
from src.orchestration.cycle_snapshot import AccountSnapshot
from src.utilities.clock import BacktestClock, ClockABC, LiveClock
from src.utilities.logger import setup_logger

logger = setup_logger("AlpacaHistoricalBroker")

_STOCK_BASE  = "https://data.alpaca.markets/v2"
_OPTION_BASE = "https://data.alpaca.markets/v1beta1"


@dataclass
class AlpacaHistoricalBroker(BrokerBase, BrokerABC):
    """
    BrokerABC for paper testing and backtesting.

    get_account_snapshot() returns a typed AccountSnapshot — equity, cash,
    option_buying_power, positions, open_orders in one call.
    CycleSnapshotBuilder no longer needs separate get_equity() /
    get_positions() / get_open_orders() calls.
    """

    account:          SimulatedAccount
    api_key:          str
    api_secret:       str
    clock:            ClockABC = field(default_factory=LiveClock)
    rate_limit_sleep: float    = 0.15

    _session:     Optional[Session]               = field(init=False, default=None)
    _chain_cache: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict, init=False)
    _bar_cache:   Dict[str, float]                = field(default_factory=dict, init=False)

    # ------------------------------------------------------------------
    # Date control
    # ------------------------------------------------------------------

    def set_date(self, d: date) -> None:
        if not isinstance(self.clock, BacktestClock):
            raise TypeError(
                "set_date() requires a BacktestClock. "
                "Did you forget to pass clock=BacktestClock() when constructing the broker?"
            )
        self.clock.set_date(d)
        self._chain_cache.clear()
        self._bar_cache.clear()
        logger.info("BacktestBroker date set | date=%s", d.isoformat())

    @property
    def _current_date(self) -> date:
        return self.clock.today()

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
    # ExecutionBrokerABC — account
    # ------------------------------------------------------------------

    def get_account_snapshot(self) -> AccountSnapshot:
        """One call returns all account state as a typed value object."""
        return AccountSnapshot(
            equity=self.account.equity,
            cash=self.account.cash,
            option_buying_power=self.account.option_buying_power,
            positions=self.account.positions(),
            open_orders=[],   # all orders fill same-day in simulation
        )

    def submit_order(self, order: OrderABC) -> Any:
        date_str = self._current_date.isoformat()
        if isinstance(order, MultiLegLimitOrder):
            return self._fill_mleg(order, date_str)
        if isinstance(order, MarketOrder):
            return self._fill_market(order, date_str)
        raise TypeError(f"Unsupported order type: {type(order).__name__}")

    def cancel_order(self, order_id: str) -> None:
        pass

    # ------------------------------------------------------------------
    # MarketDataProviderABC
    # ------------------------------------------------------------------

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        sym   = symbol.strip().upper()
        price = self._get_bar_close(sym)
        return AssetQuote(
            symbol=sym,
            bid=price,
            ask=price,
            mid=price,
            timestamp_utc=self.clock.now_utc(),
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

        params: Dict[str, Any] = {"feed": "indicative"}
        if include_calls and not include_puts:
            params["type"] = "call"
        elif include_puts and not include_calls:
            params["type"] = "put"

        cache_key = f"{sym}:{self._current_date}:{params.get('type', '')}"

        if cache_key not in self._chain_cache:
            import sys as _sys
            raw_chain: List[Dict[str, Any]] = []
            next_page: Optional[str] = None
            page_num = 0
            _MAX_RAW = 3000

            _sys.stdout.write(f"\r  Fetching {sym} chain... 0 contracts")
            _sys.stdout.flush()

            while True:
                p = dict(params)
                if next_page:
                    p["page_token"] = next_page
                data = self._get(url, params=p)
                if not isinstance(data, dict):
                    raise BrokerConnectionError(
                        "alpaca-historical", f"Unexpected response for {sym}"
                    )
                page_num += 1
                for contract_sym, snap in (data.get("snapshots") or {}).items():
                    if isinstance(snap, dict):
                        row = dict(snap)
                        row["contract_symbol"] = contract_sym
                        raw_chain.append(row)

                _sys.stdout.write(
                    f"\r  Fetching {sym} chain... {len(raw_chain)} contracts (page {page_num})"
                )
                _sys.stdout.flush()

                if len(raw_chain) >= _MAX_RAW:
                    logger.warning(
                        "Chain fetch capped at %d contracts for %s",
                        _MAX_RAW, sym,
                    )
                    break

                nxt = data.get("next_page_token")
                next_page = (
                    str(nxt).strip()
                    if isinstance(nxt, str) and nxt.strip()
                    else None
                )
                if not next_page:
                    break

            _sys.stdout.write(
                f"\r  Chain fetched: {sym} — {len(raw_chain)} contracts ({page_num} pages)\n"
            )
            _sys.stdout.flush()

            logger.info(
                "Chain fetched | sym=%s date=%s contracts=%d pages=%d",
                sym, self._current_date, len(raw_chain), page_num,
            )
            self._chain_cache[cache_key] = raw_chain
        else:
            logger.info("Chain cache hit | key=%s", cache_key)

        from src.risk.pmcc_sizer import parse_osi

        filtered: List[Dict[str, Any]] = []
        for row in self._chain_cache[cache_key]:
            sym_str = str(row.get("contract_symbol", "")).strip().upper()
            if not sym_str:
                continue
            try:
                parsed = parse_osi(sym_str)
            except ValueError:
                continue

            exp    = parsed.expiry.date()
            strike = float(parsed.strike)

            if expiration_date     is not None and exp    != expiration_date:     continue
            if expiration_date_gte is not None and exp     < expiration_date_gte: continue
            if expiration_date_lte is not None and exp     > expiration_date_lte: continue
            if strike_price_gte    is not None and strike  < strike_price_gte:    continue
            if strike_price_lte    is not None and strike  > strike_price_lte:    continue

            filtered.append(row)
            if 0 < limit <= len(filtered):
                break

        logger.info(
            "Chain filtered | sym=%s total=%d filtered=%d expiry=[%s → %s]",
            sym, len(self._chain_cache[cache_key]), len(filtered),
            expiration_date_gte, expiration_date_lte,
        )
        return filtered

    # ------------------------------------------------------------------
    # Fill simulation
    # ------------------------------------------------------------------

    def _fill_mleg(self, order: MultiLegLimitOrder, date_str: str) -> None:
        underlying = str(order.underlying).upper()
        leg_fills: List[Dict[str, Any]] = []

        for leg in order.legs:
            sym   = str(leg.contract.option_symbol).upper()
            price = self._get_option_price(sym, leg.side)
            if price is None:
                logger.warning("MLEG fill skipped — no price for %s on %s", sym, date_str)
                return
            leg_fills.append({
                "symbol": sym, "side": leg.side,
                "price": price, "underlying": underlying,
            })

        net_debit = sum(
            f["price"] if f["side"] == DomainOrderSide.BUY else -f["price"]
            for f in leg_fills
        )
        limit = float(order.limit_price)
        if limit > 0 and net_debit > limit * 1.02:
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
        sym        = str(order.symbol).upper()
        underlying = self._underlying_from_osi(sym)
        price      = self._get_option_price(sym, order.side)

        if price is None:
            logger.warning("Market fill skipped — no price for %s on %s", sym, date_str)
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
    # Price helpers
    # ------------------------------------------------------------------

    def _get_option_price(
        self,
        osi_symbol: str,
        side: DomainOrderSide,
    ) -> Optional[float]:
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

        logger.warning(
            "Option not in chain cache — fetching full snapshot | sym=%s", osi_symbol
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
        if symbol in self._bar_cache:
            return self._bar_cache[symbol]

        end   = self._current_date
        start = (end - timedelta(days=5)).isoformat()
        url   = f"{_STOCK_BASE}/stocks/{symbol}/bars"
        data  = self._get(url, params={
            "timeframe":  "1Day",
            "start":      start,
            "end":        end.isoformat(),
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