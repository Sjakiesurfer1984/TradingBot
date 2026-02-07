from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypeVar, TYPE_CHECKING, cast
from decimal import ROUND_HALF_UP, Decimal

from TradingBot.v2.brokers.broker_interface import BrokerInterface
from TradingBot.v2.domain.orders import MultiLegLimitOrder, OptionLeg as DomainOptionLeg, OrderSide as DomainOrderSide, TimeInForce as DomainTIF

import time
from datetime import date, datetime, timezone

import requests
from requests import Session
from requests.adapters import HTTPAdapter

from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
from TradingBot.v2.brokers.broker_base import BrokerBase
from TradingBot.v2.brokers.errors import BrokerConnectionError
from TradingBot.v2.domain.types import AssetQuote, ClientOrderId
from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

import json
import os

logger = setup_logger("AlpacaBroker")

TOrder = TypeVar("TOrder")

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import OrderRequest
    from alpaca.trading.models import Order as AlpacaOrder
    
@dataclass
class AlpacaBroker(BrokerBase, BrokerInterface):
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
    _trading_client: Optional["TradingClient"] = field(default=None, init=False, repr=False)


    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------

    def _base_url(self) -> str:
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    def _base_url_v2(self) -> str:
        return f"{self._base_url()}/v2"
    
    def _option_data_base_url(self) -> str:
        """
        Base URL for Alpaca option market data.

        Why this exists
        - Alpaca option data is served from data.alpaca.markets,
          but under different paths and versioning than equity data.
        - Prevents accidental coupling between stock and option endpoints.
        - Allows fast migration if Alpaca moves option data again.
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

    def _ensure_trading_client(self) -> "TradingClient":
        """
        Create and cache the Alpaca TradingClient instance.

        Why this exists
        - TradingClient manages auth + session configuration for the Alpaca SDK.
        - Creating it repeatedly is unnecessary and makes behaviour harder to reason about.
        """
        if self._trading_client is not None:
            return self._trading_client

        with log_scope("alpaca_broker._ensure_trading_client", logger, extra=f"paper={self.paper}"):
            try:
                from alpaca.trading.client import TradingClient  # type: ignore
            except Exception as exc:
                raise RuntimeError("alpaca-py is not installed or import failed") from exc

            self._trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=self.paper,
            )

            logger.info("Created Alpaca TradingClient paper=%s", bool(self.paper))
            return self._trading_client

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

    def _quantise_money_2dp(self, value: Decimal) -> Decimal:
        """
        Quantise a Decimal money value to 2 decimal places.

        Why this exists
        - Alpaca requires limit_price to be 2 decimal places for order submission.
        - Domain can keep higher precision, broker adapts at the boundary.
        """
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    
    def _to_alpaca_order_request(self, order: Any) -> "OrderRequest":
        """
        Convert our broker-agnostic domain order objects into Alpaca request objects.

        Supported now
        - MultiLegLimitOrder (options multi-leg limit order via Alpaca OrderClass.MLEG)

        Important
        - We require each OptionContract to already have contract.option_symbol populated.
        This keeps strategy and risk broker-agnostic, while making the broker adapter
        responsible for submitting only execution-ready orders.
        """

        
        try:
            from alpaca.trading.enums import OrderClass, OrderType, TimeInForce as AlpacaTIF, OrderSide as AlpacaSide
            from alpaca.trading.requests import OrderRequest, LimitOrderRequest, OptionLegRequest
        except Exception as exc:
            raise RuntimeError("alpaca-py is not installed or import failed") from exc

        # Passthrough for already-constructed Alpaca requests (useful during migration).
        if isinstance(order, OrderRequest):
            return order

        if not isinstance(order, MultiLegLimitOrder):
            raise TypeError(
                f"Unsupported internal order type for Alpaca mapping: {type(order).__name__}. "
                "Expected MultiLegLimitOrder."
            )

        if order.quantity <= 0:
            raise ValueError(f"MultiLegLimitOrder.quantity must be > 0, got {order.quantity}")

        if not isinstance(order.limit_price, Decimal):
            raise ValueError(
                f"MultiLegLimitOrder.limit_price must be Decimal, got {type(order.limit_price).__name__}"
            )

        if not order.client_order_id or not str(order.client_order_id).strip():
            raise ValueError("MultiLegLimitOrder.client_order_id must be a non-empty string")

        # Map TIF
        tif_map: dict[DomainTIF, AlpacaTIF] = {
            DomainTIF.DAY: AlpacaTIF.DAY,
            DomainTIF.GTC: AlpacaTIF.GTC,
        }
        alpaca_tif: AlpacaTIF = tif_map[order.time_in_force]

        # Map legs
        alpaca_legs: list[OptionLegRequest] = []
        for idx, leg in enumerate(order.legs):
            if not isinstance(leg, DomainOptionLeg):
                raise ValueError(
                    f"Order legs must be DomainOptionLeg, got {type(leg).__name__} at index {idx}"
                )

            if leg.ratio <= 0:
                raise ValueError(f"OptionLeg.ratio must be > 0, got {leg.ratio} at index {idx}")

            option_symbol: Optional[str] = leg.contract.option_symbol
            if option_symbol is None or not str(option_symbol).strip():
                raise ValueError(
                    "OptionContract.option_symbol is required for Alpaca submission. "
                    f"Missing on leg index {idx} (underlying={leg.contract.underlying})."
                )

            if leg.side == DomainOrderSide.BUY:
                alpaca_side: AlpacaSide = AlpacaSide.BUY
            elif leg.side == DomainOrderSide.SELL:
                alpaca_side = AlpacaSide.SELL
            else:
                raise ValueError(f"Unsupported leg.side value: {leg.side!r}")

            alpaca_legs.append(
                OptionLegRequest(
                    symbol=str(option_symbol).strip().upper(),
                    side=alpaca_side,
                    ratio_qty=int(leg.ratio),
                )
            )

        if len(alpaca_legs) < 2:
            raise ValueError("Multi-leg options order must have at least 2 legs for Alpaca MLEG submission")

        limit_price_2dp: Decimal = self._quantise_money_2dp(order.limit_price)

        if limit_price_2dp != order.limit_price:
            logger.info(
                "Alpaca limit_price quantised to 2dp client_order_id=%s before=%s after=%s",
                str(order.client_order_id),
                str(order.limit_price),
                str(limit_price_2dp),
            )

        request = LimitOrderRequest(
            type=OrderType.LIMIT,
            order_class=OrderClass.MLEG,
            qty=float(order.quantity),
            limit_price=float(limit_price_2dp),
            time_in_force=alpaca_tif,
            client_order_id=str(order.client_order_id),
            legs=alpaca_legs,
        )

        return request

    # ---------------------------------------------------------------------
    # BrokerInterface implementation
    # ---------------------------------------------------------------------
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
        
    def get_option_buying_power(self) -> float:
        self._log_io_boundary("get_option_buying_power")
        with log_scope("broker.get_option_buying_power", logger):
            logger.info("Broker call get_option_buying_power")
            data: Dict[str, Any] = self._request_json("GET", "/account")
            raw: Any = data.get("options_buying_power")
            obp: float = self._safe_float(raw, default=0.0)
            logger.info("Broker result get_option_buying_power options_buying_power=%.2f", float(obp))
            return obp
        
    def get_equity(self) -> float:
        self._log_io_boundary("get_equity")
        with log_scope("broker.get_equity", logger):
            logger.info("Broker call get_equity")
            data: Dict[str, Any] = self._request_json("GET", "/account")
            equity: float = self._safe_float(data.get("equity"), default=0.0)
            logger.info("Broker result get_equity equity=%.2f", float(equity))
            return max(equity, 0.0)

    def get_positions(self) -> List[Dict[str, Any]]:
        self._log_io_boundary("get_positions")
        with log_scope("broker.get_positions", logger):
            # -----------------------------
            # TEST OVERRIDE (local only)
            # -----------------------------
            enabled_raw: str = str(os.getenv("TBOT_TEST_POSITIONS_ENABLED", "")).strip().lower()
            test_enabled: bool = enabled_raw in {"1", "true", "yes", "y", "on"}

            if test_enabled:
                raw_json: str = str(os.getenv("TBOT_TEST_POSITIONS_JSON", "")).strip()
                if raw_json:
                    try:
                        positions_any: Any = json.loads(raw_json)
                        if isinstance(positions_any, list):
                            positions: List[Dict[str, Any]] = [p for p in positions_any if isinstance(p, dict)]
                            logger.warning(
                                "TEST POSITIONS OVERRIDE ACTIVE | returning mocked positions | count=%d",
                                int(len(positions)),
                            )
                            return positions
                        logger.warning("TBOT_TEST_POSITIONS_JSON is not a list; ignoring override")
                    except Exception as exc:
                        logger.warning("Failed to parse TBOT_TEST_POSITIONS_JSON; ignoring override | error=%s", str(exc))

            # Normal behaviour (real broker IO)
            logger.info("Broker call get_positions")
            positions_any: Any = self._request_json("GET", "/positions")

            if not isinstance(positions_any, list):
                logger.warning("Broker result get_positions unexpected_type=%s", type(positions_any).__name__)
                return []

            positions: List[Dict[str, Any]] = [p for p in positions_any if isinstance(p, dict)]
            logger.info("Broker result get_positions count=%d", int(len(positions)))
            return positions

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

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        """
        Fetch the latest quote (bid/ask) for a stock symbol.

        Why this is implemented this way
        - We reuse the same HTTP plumbing as trading calls (session reuse, strict timeouts, consistent logs).
        - We explicitly request the IEX feed, which is the normal feed for paper accounts.
        - We validate payload shape aggressively.
        - We compute a mid only when both bid and ask are usable.
        """
        self._log_io_boundary("get_asset_quote")

        sym: str = symbol.strip().upper()
        if not sym:
            raise ValueError("symbol must be a non-empty string")

        url: str = f"https://data.alpaca.markets/v2/stocks/{sym}/quotes/latest"
        params: Dict[str, Any] = {"feed": "iex"}

        with log_scope(
            "broker.get_asset_quote",
            logger,
            extra=f"symbol={sym} url={url} params={params}",
        ):
            logger.info("Broker call get_asset_quote symbol=%s", sym)

            data_any: Any = self._request_json_url("GET", url, params=params)

            if not isinstance(data_any, dict):
                logger.error(
                    "Market data unexpected_type symbol=%s type=%s",
                    sym,
                    type(data_any).__name__,
                )
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"Unexpected market data response type for {sym}: {type(data_any).__name__}",
                )

            quote_any: Any = data_any.get("quote")
            if not isinstance(quote_any, dict):
                logger.error(
                    "Market data missing_quote symbol=%s payload=%s",
                    sym,
                    data_any,
                )
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"Market data response missing 'quote' for {sym}.",
                )

            ask: float = self._safe_float(quote_any.get("ap"), default=0.0)
            bid: float = self._safe_float(quote_any.get("bp"), default=0.0)

            mid: Optional[float] = None

            if ask > 0.0 and bid > 0.0:
                mid = (ask + bid) / 2.0
                logger.info(
                    "Broker result get_asset_quote symbol=%s bid=%.4f ask=%.4f mid=%.4f",
                    sym,
                    bid,
                    ask,
                    mid,
                )

            elif ask > 0.0:
                logger.info(
                    "Broker result get_asset_quote symbol=%s ask_only=%.4f",
                    sym,
                    ask,
                )

            elif bid > 0.0:
                logger.info(
                    "Broker result get_asset_quote symbol=%s bid_only=%.4f",
                    sym,
                    bid,
                )

            else:
                logger.error(
                    "Broker result get_asset_quote no_usable_quote symbol=%s quote=%s",
                    sym,
                    quote_any,
                )
                raise BrokerConnectionError(
                    broker_name="alpaca",
                    message=f"No usable quote returned for {sym}.",
                )

            return AssetQuote(
                symbol=sym,
                bid=bid if bid > 0.0 else None,
                ask=ask if ask > 0.0 else None,
                mid=mid,
                timestamp_utc=None,  # Alpaca quote timestamp can be added later if desired
            )

    # Get option chain
    def get_option_chain(
        self,
        underlying: str,
        *,
        include_calls: bool = True,
        include_puts: bool = False,
        feed: str = "indicative",
        max_age_seconds: int = 30,
        limit: int = 0,
        strike_price_gte: Optional[float] = None,
        strike_price_lte: Optional[float] = None,
        expiration_date: Optional[date] = None,
        expiration_date_gte: Optional[date] = None,
        expiration_date_lte: Optional[date] = None,
        root_symbol: Optional[str] = None,
        updated_since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch the option chain snapshot for a single underlying.

        Contract
        - Broker IO only.
        - One underlying in, flat list of contracts out.
        - No strategy logic, no orchestration logic.

        Data source
        - GET /v1beta1/options/snapshots/{underlying}

        Notes
        - OPRA may be forbidden on paper accounts.
        - Indicative feed is delayed and must be logged loudly.
        """

        self._log_io_boundary("get_option_chain")

        sym: str = str(underlying).strip().upper()
        if not sym:
            raise ValueError("underlying must be a non-empty string")

        if not include_calls and not include_puts:
            raise ValueError("At least one of include_calls or include_puts must be True")

        feed_norm: str = str(feed).strip().lower()
        if feed_norm not in {"opra", "indicative"}:
            raise ValueError("feed must be 'opra' or 'indicative'")

        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be > 0")

        if limit < 0:
            raise ValueError("limit must be >= 0")

        base: str = self._option_data_base_url()
        url: str = f"{base}/v1beta1/options/snapshots/{sym}"

        params: Dict[str, Any] = {"feed": feed_norm}

        # Filter: calls/puts
        if include_calls and not include_puts:
            params["type"] = "call"
        elif include_puts and not include_calls:
            params["type"] = "put"
        # else: both, do not set "type" and let the API return both

        # Filters: strikes
        if strike_price_gte is not None:
            params["strike_price_gte"] = float(strike_price_gte)
        if strike_price_lte is not None:
            params["strike_price_lte"] = float(strike_price_lte)

        # Filters: expirations
        def _date_to_str(d: date) -> str:
            return d.isoformat()

        if expiration_date is not None:
            params["expiration_date"] = _date_to_str(expiration_date)
        if expiration_date_gte is not None:
            params["expiration_date_gte"] = _date_to_str(expiration_date_gte)
        if expiration_date_lte is not None:
            params["expiration_date_lte"] = _date_to_str(expiration_date_lte)

        # Filter: root symbol
        if root_symbol is not None and str(root_symbol).strip():
            params["root_symbol"] = str(root_symbol).strip().upper()

        # Filter: updated_since (UTC ISO8601)
        if updated_since is not None:
            params["updated_since"] = (
                updated_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )

        with log_scope(
            "broker.get_option_chain",
            logger,
            extra=f"underlying={sym} url={url} params={params}",
        ):
            logger.info(
                "Broker call get_option_chain | underlying=%s include_calls=%s include_puts=%s feed=%s params=%s",
                sym,
                bool(include_calls),
                bool(include_puts),
                feed_norm,
                dict(params),
            )

            chain: List[Dict[str, Any]] = []
            newest_ts: Optional[datetime] = None

            next_page_token: Optional[str] = None
            page_index: int = 0

            while True:
                page_params: Dict[str, Any] = dict(params)
                if next_page_token:
                    page_params["page_token"] = next_page_token

                data_any: Any = self._request_json_url("GET", url, params=page_params)

                if not isinstance(data_any, dict):
                    raise BrokerConnectionError(
                        broker_name="alpaca",
                        message=f"Unexpected option chain response type for {sym}",
                    )

                snapshots_any: Any = data_any.get("snapshots")
                if not isinstance(snapshots_any, dict):
                    raise BrokerConnectionError(
                        broker_name="alpaca",
                        message=f"Missing snapshots in option chain response for {sym}",
                    )

                for contract_symbol, snap_any in snapshots_any.items():
                    if not isinstance(snap_any, dict):
                        continue

                    row: Dict[str, Any] = dict(snap_any)
                    row["contract_symbol"] = str(contract_symbol)

                    # Track the newest timestamp we can find (for staleness detection).
                    for key in ("latestQuote", "latestTrade"):
                        block: Any = row.get(key)
                        if isinstance(block, dict):
                            ts_any: Any = block.get("t")
                            if isinstance(ts_any, str):
                                try:
                                    ts_dt: datetime = datetime.fromisoformat(
                                        ts_any.replace("Z", "+00:00")
                                    )
                                    if newest_ts is None or ts_dt > newest_ts:
                                        newest_ts = ts_dt
                                except Exception:
                                    pass

                    chain.append(row)

                    if limit > 0 and len(chain) >= limit:
                        break

                if limit > 0 and len(chain) >= limit:
                    break

                next_any: Any = data_any.get("next_page_token")
                next_page_token = (
                    str(next_any).strip() if isinstance(next_any, str) and next_any.strip() else None
                )

                logger.info(
                    "Option chain page | underlying=%s page=%d page_contracts=%d total=%d next=%s",
                    sym,
                    page_index,
                    int(len(snapshots_any)),
                    int(len(chain)),
                    "yes" if next_page_token else "no",
                )

                if not next_page_token:
                    break

                page_index += 1

            logger.info(
                "Option chain fetched | underlying=%s contracts=%d feed=%s",
                sym,
                int(len(chain)),
                feed_norm,
            )

            if feed_norm == "indicative":
                logger.warning(
                    "Option chain feed is indicative | underlying=%s quotes_delayed=true",
                    sym,
                )

            # Compute staleness once, stamp onto every row so the orchestrator can check row[0].
            newest_ts_str: str = newest_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if newest_ts else ""
            is_stale: bool = False

            if newest_ts is not None:
                now_utc: datetime = datetime.now(timezone.utc)
                age_seconds: float = float((now_utc - newest_ts).total_seconds())
                if age_seconds > float(max_age_seconds):
                    is_stale = True
                    logger.warning(
                        "Option chain stale | underlying=%s age_seconds=%.1f threshold=%d newest_ts=%s",
                        sym,
                        float(age_seconds),
                        int(max_age_seconds),
                        newest_ts_str,
                    )

            for row in chain:
                row["_feed"] = feed_norm
                row["_newest_ts"] = newest_ts_str
                row["_is_stale"] = bool(is_stale)

            return chain
        
    def submit_order(self, order: TOrder) -> "AlpacaOrder":
        """
        Submit an order to Alpaca.

        Parameters
        - order: internal executable order (for example MultiLegLimitOrder)

        Returns
        - Alpaca Order model returned by alpaca-py
        """
        self._log_io_boundary("submit_order")

        with log_scope("alpaca_broker.submit_order", logger):
            client_order_id: Optional[ClientOrderId] = getattr(order, "client_order_id", None)

            logger.info(
                "Submitting order to Alpaca client_order_id=%s order_type=%s",
                client_order_id,
                type(order).__name__,
            )

            order_request = self._to_alpaca_order_request(order)

            # This log is useful when debugging request-shape issues.
            # Many alpaca-py request objects are Pydantic models and support model_dump().
            request_payload: Any
            if hasattr(order_request, "model_dump"):
                request_payload = order_request.model_dump()
            elif hasattr(order_request, "dict"):
                request_payload = order_request.dict()
            else:
                request_payload = str(order_request)

            logger.info(
                "Alpaca submit_order request client_order_id=%s payload=%s",
                client_order_id,
                request_payload,
            )

            trading_client = self._ensure_trading_client()
            response_any: Any = trading_client.submit_order(order_data=order_request)

            logger.info(
                "Alpaca submit_order response client_order_id=%s alpaca_order_id=%s status=%s",
                client_order_id,
                getattr(response_any, "id", None) if not isinstance(response_any, dict) else response_any.get("id"),
                getattr(response_any, "status", None) if not isinstance(response_any, dict) else response_any.get("status"),
            )

            if isinstance(response_any, dict):
                raise TypeError(
                    "Alpaca submit_order returned raw dict payload. "
                    "This violates the broker contract. "
                    f"payload_keys={list(response_any.keys())}"
                )

            return cast("AlpacaOrder", response_any)

