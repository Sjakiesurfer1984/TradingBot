from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import requests
from requests import Session
from requests.adapters import HTTPAdapter

from src.brokers.broker_base import BrokerBase
from src.brokers.errors import BrokerConnectionError
from src.brokers.interfaces import BrokerABC
from src.domain.orders import (
    MarketOrder,
    MultiLegLimitOrder,
    OrderABC,
    OrderSide as DomainOrderSide,
    TimeInForce as DomainTIF,
)
from src.domain.types import AssetQuote
from src.utilities.logger import setup_logger

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

logger = setup_logger("AlpacaBroker")


class AlpacaBroker(BrokerBase, BrokerABC):

    def __init__(
        self,
        *,
        api_key:                 str,
        api_secret:              str,
        paper:                   bool  = True,
        request_timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 3.0,
    ) -> None:
        self._api_key           = api_key
        self._api_secret        = api_secret
        self._paper             = paper
        self._request_timeout   = request_timeout_seconds
        self._connect_timeout   = connect_timeout_seconds
        self._session: Optional[Session]               = None
        self._trading_client: Optional["TradingClient"] = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _base_url_v2(self) -> str:
        base = "https://paper-api.alpaca.markets" if self._paper else "https://api.alpaca.markets"
        return f"{base}/v2"

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID":     self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
        }

    def _ensure_session(self) -> Session:
        if self._session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://",  adapter)
            self._session = session
            logger.info("HTTP session created | paper=%s", self._paper)
        return self._session

    def _ensure_trading_client(self) -> "TradingClient":
        if self._trading_client is None:
            from alpaca.trading.client import TradingClient
            self._trading_client = TradingClient(
                api_key=self._api_key,
                secret_key=self._api_secret,
                paper=self._paper,
            )
        return self._trading_client

    def _request_json(self, method: str, path: str, *, params: Optional[Dict] = None) -> Any:
        return self._request_json_url(method, self._base_url_v2() + path, params=params)

    def _request_json_url(self, method: str, url: str, *, params: Optional[Dict] = None) -> Any:
        session  = self._ensure_session()
        timeout  = (self._connect_timeout, self._request_timeout)
        response = session.request(method, url, headers=self._headers(), params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _quantise_2dp(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))

    # ------------------------------------------------------------------
    # ExecutionBrokerABC
    # ------------------------------------------------------------------

    def get_account_snapshot(self) -> Dict[str, Any]:
        self._log_io_boundary("get_account_snapshot")
        return self._request_json("GET", "/account")  # type: ignore[return-value]

    def get_option_buying_power(self) -> float:
        return self._safe_float(self.get_account_snapshot().get("options_buying_power"))

    def get_equity(self) -> float:
        return self._safe_float(self.get_account_snapshot().get("equity"))

    def get_positions(self) -> List[Dict[str, Any]]:
        self._log_io_boundary("get_positions")
        data = self._request_json("GET", "/positions")
        positions = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
        # Log every position so we can see exactly what Alpaca returns
        for p in positions:
            logger.info(
                "Position | symbol=%s asset_class=%s underlying=%s qty=%s side=%s",
                p.get("symbol"), p.get("asset_class"),
                p.get("underlying_symbol"), p.get("qty"), p.get("side"),
            )
        if not positions:
            logger.info("Positions | none")
        return positions

    def get_open_orders(self) -> List[Dict[str, Any]]:
        self._log_io_boundary("get_open_orders")
        data = self._request_json("GET", "/orders", params={"status": "open"})
        orders = [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []
        # Log every open order so we can see exactly what Alpaca returns
        for o in orders:
            legs_info = [
                f"{leg.get('symbol')}:{leg.get('side')}"
                for leg in (o.get("legs") or [])
                if isinstance(leg, dict)
            ]
            logger.info(
                "OpenOrder | id=%s symbol=%s underlying=%s type=%s status=%s legs=%s",
                o.get("id"), o.get("symbol"), o.get("underlying_symbol"),
                o.get("order_class"), o.get("status"), legs_info or "n/a",
            )
        if not orders:
            logger.info("OpenOrders | none")
        return orders

    def submit_order(self, order: OrderABC) -> Any:
        self._log_io_boundary("submit_order")
        if isinstance(order, MultiLegLimitOrder):
            return self._ensure_trading_client().submit_order(order_data=self._to_alpaca_mleg(order))
        if isinstance(order, MarketOrder):
            return self._ensure_trading_client().submit_order(order_data=self._to_alpaca_market(order))
        raise TypeError(f"Unsupported order type: {type(order).__name__}")

    def cancel_order(self, order_id: str) -> None:
        self._log_io_boundary("cancel_order")
        self._request_json("DELETE", f"/orders/{order_id.strip()}")

    # ------------------------------------------------------------------
    # MarketDataProviderABC
    # ------------------------------------------------------------------

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        self._log_io_boundary("get_asset_quote")
        sym  = symbol.strip().upper()
        url  = f"https://data.alpaca.markets/v2/stocks/{sym}/quotes/latest"
        data = self._request_json_url("GET", url, params={"feed": "iex"})
        if not isinstance(data, dict):
            raise BrokerConnectionError("alpaca", f"Unexpected response for {sym}")
        q = data.get("quote")
        if not isinstance(q, dict):
            raise BrokerConnectionError("alpaca", f"Missing 'quote' for {sym}")
        ask = self._safe_float(q.get("ap"))
        bid = self._safe_float(q.get("bp"))
        mid = (ask + bid) / 2.0 if ask > 0 and bid > 0 else None
        return AssetQuote(symbol=sym, bid=bid or None, ask=ask or None, mid=mid, timestamp_utc=None)

    def get_latest_price(self, symbol: str) -> float:
        q = self.get_asset_quote(symbol)
        for v in (q.mid, q.ask, q.bid):
            if isinstance(v, float) and v > 0:
                return v
        raise BrokerConnectionError("alpaca", f"No usable price for {symbol}")

    def get_daily_bars(self, symbol: str, lookback_days: int) -> Any:
        self._log_io_boundary("get_daily_bars")
        sym   = symbol.strip().upper()
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days + 5)
        url   = f"https://data.alpaca.markets/v2/stocks/{sym}/bars"
        return self._request_json_url("GET", url, params={
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
        self._log_io_boundary("get_option_chain")
        sym    = underlying.strip().upper()
        url    = f"https://data.alpaca.markets/v1beta1/options/snapshots/{sym}"
        params: Dict[str, Any] = {"feed": feed.strip().lower()}

        if include_calls and not include_puts:
            params["type"] = "call"
        elif include_puts and not include_calls:
            params["type"] = "put"
        if strike_price_gte is not None:
            params["strike_price_gte"] = strike_price_gte
        if strike_price_lte is not None:
            params["strike_price_lte"] = strike_price_lte
        if expiration_date is not None:
            params["expiration_date"] = expiration_date.isoformat()
        if expiration_date_gte is not None:
            params["expiration_date_gte"] = expiration_date_gte.isoformat()
        if expiration_date_lte is not None:
            params["expiration_date_lte"] = expiration_date_lte.isoformat()
        if root_symbol:
            params["root_symbol"] = root_symbol.strip().upper()
        if updated_since is not None:
            params["updated_since"] = (
                updated_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )

        chain: List[Dict[str, Any]] = []
        next_page: Optional[str] = None

        while True:
            p = dict(params)
            if next_page:
                p["page_token"] = next_page
            data = self._request_json_url("GET", url, params=p)
            if not isinstance(data, dict):
                raise BrokerConnectionError("alpaca", f"Unexpected chain response for {sym}")
            for contract_sym, snap in (data.get("snapshots") or {}).items():
                if isinstance(snap, dict):
                    row = dict(snap)
                    row["contract_symbol"] = contract_sym
                    chain.append(row)
                    if 0 < limit <= len(chain):
                        return chain
            nxt = data.get("next_page_token")
            next_page = str(nxt).strip() if isinstance(nxt, str) and nxt.strip() else None
            if not next_page:
                break

        return chain

    # ------------------------------------------------------------------
    # Private Alpaca order builders
    # ------------------------------------------------------------------

    def _to_alpaca_mleg(self, order: MultiLegLimitOrder) -> Any:
        try:
            from alpaca.trading.enums import OrderClass, OrderType
            from alpaca.trading.enums import TimeInForce as AlpacaTIF, OrderSide as AlpacaSide
            from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        except ImportError as exc:
            raise RuntimeError("alpaca-py is not installed") from exc

        tif  = AlpacaTIF.DAY if order.time_in_force == DomainTIF.DAY else AlpacaTIF.GTC
        legs = [
            OptionLegRequest(
                symbol=str(leg.contract.option_symbol).strip().upper(),
                side=AlpacaSide.BUY if leg.side == DomainOrderSide.BUY else AlpacaSide.SELL,
                ratio_qty=int(leg.ratio),
            )
            for leg in order.legs
        ]
        if len(legs) < 2:
            raise ValueError("MLEG order requires at least 2 legs")

        return LimitOrderRequest(
            type=OrderType.LIMIT,
            order_class=OrderClass.MLEG,
            qty=float(order.quantity),
            limit_price=float(self._quantise_2dp(order.limit_price)),
            time_in_force=tif,
            client_order_id=str(order.client_order_id),
            legs=legs,
        )

    def _to_alpaca_market(self, order: MarketOrder) -> Any:
        try:
            from alpaca.trading.enums import TimeInForce as AlpacaTIF, OrderSide as AlpacaSide
            from alpaca.trading.requests import MarketOrderRequest
        except ImportError as exc:
            raise RuntimeError("alpaca-py is not installed") from exc

        return MarketOrderRequest(
            symbol=order.symbol,
            qty=order.quantity,
            side=AlpacaSide.BUY if order.side == DomainOrderSide.BUY else AlpacaSide.SELL,
            time_in_force=AlpacaTIF.DAY if order.time_in_force == DomainTIF.DAY else AlpacaTIF.GTC,
        )