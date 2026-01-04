from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import time

import requests
from requests import Session
from requests.adapters import HTTPAdapter

from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2
from TradingBot.v2.brokers.errors import BrokerConnectionError
from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

logger = setup_logger("AlpacaBroker")


@dataclass
class AlpacaBrokerV2(BrokerInterfaceV2):
    """
    Alpaca broker adapter for TradingBot V2.

    What this file optimises for
    - Strictly bounded network IO where possible using timeouts.
    - Connection reuse via requests.Session to reduce repeated TLS handshakes.
    - High visibility logs for diagnosing stalls, timeouts, and intermittent network issues.

    Important reality
    - If the OS networking stack or a security product stalls the TCP connect,
      Python can appear to hang until the socket layer times out or the process is interrupted.
      Logging makes the stall location obvious.
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    paper: bool = True
    request_timeout_seconds: float = 10.0

    # Connect timeout is kept smaller to fail fast when connect stalls.
    connect_timeout_seconds: float = 3.0

    # When True, logs include more detail per request.
    debug_http: bool = True

    _session: Optional[Session] = field(default=None, init=False)

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    def _base_url(self) -> str:
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    def _base_url_v2(self) -> str:
        return f"{self._base_url()}/v2"
    
    def _market_data_base_url(self) -> str:
        """
        Base URL for Alpaca Market Data API.

        Why this exists
        - Keeps market data host centralised.
        - Allows changing host/version in one place if Alpaca updates it.
        """
        return "https://data.alpaca.markets"


    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    def _ensure_session(self) -> Session:
        """
        Create and cache a requests.Session for connection pooling.

        Why this matters
        - Reusing a session reduces repeated TCP and TLS setup.
        - Reduces probability of intermittent stalls during connection establishment.
        """
        if self._session is None:
            with log_scope(
                "alpaca_broker._ensure_session",
                logger,
                extra=f"paper={self.paper} connect_timeout={self.connect_timeout_seconds:.2f}s read_timeout={self.request_timeout_seconds:.2f}s",
            ):
                session = requests.Session()

                adapter = HTTPAdapter(
                    pool_connections=4,
                    pool_maxsize=4,
                    max_retries=0,  # Do not hide issues behind retries.
                )
                session.mount("https://", adapter)
                session.mount("http://", adapter)

                self._session = session

                logger.info(
                    "Created HTTP session for Alpaca. paper=%s connect_timeout=%.2fs read_timeout=%.2fs",
                    self.paper,
                    float(self.connect_timeout_seconds),
                    float(self.request_timeout_seconds),
                )

        return self._session

    def _log_request_start(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]],
        timeout: tuple[float, float],
    ) -> None:
        if not self.debug_http:
            return

        safe_params: Dict[str, Any] = params or {}
        logger.info(
            "HTTP start method=%s url=%s params=%s timeout(connect=%.2fs, read=%.2fs)",
            method.upper(),
            url,
            safe_params,
            float(timeout[0]),
            float(timeout[1]),
        )

    def _log_request_end(self, url: str, status_code: int, elapsed_s: float) -> None:
        if not self.debug_http:
            return

        logger.info(
            "HTTP end url=%s status=%s elapsed=%.3fs",
            url,
            int(status_code),
            float(elapsed_s),
        )

    def _request_json_url(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Perform an HTTP request to a fully-qualified URL and return JSON.

        Why this exists
        - Trading endpoints use base_url_v2 + path.
        - Market data endpoints use a different host (data.alpaca.markets) and are easiest as full URLs.
        - One shared request implementation keeps behaviour consistent and reduces bugs.
        """
        session: Session = self._ensure_session()
        timeout: tuple[float, float] = (float(self.connect_timeout_seconds), float(self.request_timeout_seconds))

        extra: str = f"method={method.upper()} url={url} timeout=({timeout[0]:.2f}s,{timeout[1]:.2f}s)"
        if params:
            extra = f"{extra} params={params}"

        with log_scope("alpaca_broker._request_json_url", logger, extra=extra):
            self._log_request_start(method=method, url=url, params=params, timeout=timeout)
            t0: float = time.monotonic()

            try:
                response = session.request(
                    method=method.upper(),
                    url=url,
                    headers=self._headers(),
                    params=params,
                    timeout=timeout,
                )
                elapsed: float = time.monotonic() - t0
                self._log_request_end(url=url, status_code=int(response.status_code), elapsed_s=elapsed)

                response.raise_for_status()

                try:
                    return response.json()
                except ValueError:
                    logger.error(
                        "HTTP JSON decode failed url=%s status=%s body_prefix=%s",
                        url,
                        response.status_code,
                        response.text[:250],
                    )
                    raise

            except requests.exceptions.Timeout as exc:
                elapsed: float = time.monotonic() - t0
                logger.exception(
                    "HTTP timeout url=%s elapsed=%.3fs timeout(connect=%.2fs, read=%.2fs)",
                    url,
                    float(elapsed),
                    float(timeout[0]),
                    float(timeout[1]),
                )
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=(
                        f"Timeout calling {url}. "
                        f"Connect timeout={timeout[0]:.2f}s, read timeout={timeout[1]:.2f}s."
                    ),
                ) from exc

            except requests.exceptions.RequestException as exc:
                elapsed: float = time.monotonic() - t0
                logger.exception("HTTP request error url=%s elapsed=%.3fs", url, float(elapsed))
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"HTTP error calling {url}: {exc}",
                ) from exc

            except ValueError as exc:
                elapsed: float = time.monotonic() - t0
                logger.exception("HTTP non-JSON response url=%s elapsed=%.3fs", url, float(elapsed))
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"Non-JSON response from {url}.",
                ) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Perform a REST call to Alpaca trading endpoint with strict timeout handling.

        This method is a thin wrapper around _request_json_url for trading endpoints.
        """
        url: str = f"{self._base_url_v2()}{path}"
        return self._request_json_url(method=method, url=url, params=params)

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    # ---------------------------------------------------------------------
    # BrokerInterfaceV2 implementation
    # ---------------------------------------------------------------------

    def get_equity(self) -> float:
        self._log_io_boundary("get_equity")
        with log_scope("broker.get_equity", logger):
            logger.info("Broker call get_equity")
            data: Dict[str, Any] = self._request_json("GET", "/account")
            equity: float = self._safe_float(data.get("equity"), default=0.0)
            logger.info("Broker result get_equity equity=%.2f", float(equity))
            return max(equity, 0.0)

    def get_option_buying_power(self) -> float:
        self._log_io_boundary("get_option_buying_power")
        with log_scope("broker.get_option_buying_power", logger):
            logger.info("Broker call get_option_buying_power")
            data: Dict[str, Any] = self._request_json("GET", "/account")
            raw: Any = data.get("options_buying_power")
            obp: float = self._safe_float(raw, default=0.0)
            logger.info("Broker result get_option_buying_power options_buying_power=%.2f", float(obp))
            return obp

    def get_positions(self) -> Dict[str, Any]:
        self._log_io_boundary("get_positions")
        with log_scope("broker.get_positions", logger):
            logger.info("Broker call get_positions")
            positions: Any = self._request_json("GET", "/positions")

            result: Dict[str, Any] = {}
            if not isinstance(positions, list):
                logger.warning("Broker result get_positions unexpected_type=%s", type(positions).__name__)
                return result

            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                symbol: str = str(pos.get("symbol", "")).strip().upper()
                if symbol:
                    result[symbol] = pos

            logger.info("Broker result get_positions count=%d", int(len(result)))
            return result

    def get_open_orders(self) -> List[Dict[str, Any]]:
        self._log_io_boundary("get_open_orders")
        with log_scope("broker.get_open_orders", logger):
            logger.info("Broker call get_open_orders")
            orders: Any = self._request_json("GET", "/orders", params={"status": "open"})

            if not isinstance(orders, list):
                logger.warning("Broker result get_open_orders unexpected_type=%s", type(orders).__name__)
                return []

            filtered: List[Dict[str, Any]] = [o for o in orders if isinstance(o, dict)]
            logger.info("Broker result get_open_orders count=%d", int(len(filtered)))
            return filtered

    def submit_order(self, order: Any) -> Any:
        self._log_io_boundary("submit_order")
        raise NotImplementedError(
            "Order submission not implemented yet. "
            "Define v2/domain/orders.py request types first."
        )

    def get_asset_price(self, symbol: str) -> float:
        """
        Return a mid price for a stock using Alpaca Market Data.

        Why this is implemented this way
        - We reuse the same HTTP plumbing as trading calls (session reuse, strict timeouts, consistent logs).
        - We explicitly request the IEX feed, which is the normal feed for paper accounts.
        - We compute a mid when both bid and ask exist, with sensible fallbacks.
        """
        self._log_io_boundary("get_asset_price")

        sym: str = symbol.strip().upper()
        if not sym:
            raise ValueError("symbol must be a non-empty string")

        url: str = f"https://data.alpaca.markets/v2/stocks/{sym}/quotes/latest"
        params: Dict[str, Any] = {"feed": "iex"}

        with log_scope("broker.get_asset_price", logger, extra=f"symbol={sym} url={url} params={params}"):
            logger.info("Broker call get_asset_price symbol=%s", sym)

            data_any: Any = self._request_json_url("GET", url, params=params)

            if not isinstance(data_any, dict):
                logger.error("Market data unexpected_type symbol=%s type=%s", sym, type(data_any).__name__)
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"Unexpected market data response type for {sym}: {type(data_any).__name__}",
                )

            quote_any: Any = data_any.get("quote")
            if not isinstance(quote_any, dict):
                logger.error("Market data missing_quote symbol=%s payload=%s", sym, data_any)
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"Market data response missing 'quote' for {sym}.",
                )

            ask: float = self._safe_float(quote_any.get("ap"), default=0.0)
            bid: float = self._safe_float(quote_any.get("bp"), default=0.0)

            if ask > 0.0 and bid > 0.0:
                mid: float = (ask + bid) / 2.0
                logger.info(
                    "Broker result get_asset_price symbol=%s bid=%.4f ask=%.4f mid=%.4f",
                    sym,
                    float(bid),
                    float(ask),
                    float(mid),
                )
                return mid

            if ask > 0.0:
                logger.info("Broker result get_asset_price symbol=%s ask_only=%.4f", sym, float(ask))
                return ask

            if bid > 0.0:
                logger.info("Broker result get_asset_price symbol=%s bid_only=%.4f", sym, float(bid))
                return bid

            logger.error("Broker result get_asset_price no_usable_quote symbol=%s quote=%s", sym, quote_any)
            raise BrokerConnectionError(
                broker_name="alpaca",
                message=f"No usable quote returned for {sym}.",
            )
    
    def get_account_snapshot(self) -> AccountSnapshot:
        self._log_io_boundary("get_account_snapshot")
        with log_scope("broker.get_account_snapshot", logger):
            data: Dict[str, Any] = self._request_json("GET", "/account")

            equity: float = self._safe_float(data.get("equity"), default=0.0)
            options_buying_power: float = self._safe_float(data.get("options_buying_power"), default=0.0)
            raw_cash: Any = data.get("cash")
            cash: Optional[float] = float(raw_cash) if raw_cash is not None else None


            return AccountSnapshot(
                equity=equity,
                options_buying_power=options_buying_power,
                cash=cash,
                raw=data,
            )
