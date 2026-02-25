from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Type

from src.brokers.broker_base import BrokerBase
from src.brokers.interfaces import BrokerABC
from src.domain.orders import MarketOrder, MultiLegLimitOrder, OrderABC, OrderSide
from src.domain.types import AssetQuote
from src.orchestration.cycle_snapshot import AccountSnapshot
from src.utilities.clock import ClockABC, LiveClock
from src.utilities.logger import setup_logger

logger = setup_logger("AlpacaBroker")


@dataclass
class AlpacaBroker(BrokerABC, BrokerBase):
    api_key:    str
    secret_key: str
    paper:      bool     = True
    clock:      ClockABC = field(default_factory=LiveClock)

    _trading_client: Any = field(default=None, init=False)
    _data_client:    Any = field(default=None, init=False)
    _order_handlers: Dict[Type[OrderABC], Callable[[OrderABC], Any]] = field(
        default_factory=dict, init=False,
    )

    def __post_init__(self) -> None:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        self._trading_client = TradingClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
            paper=self.paper,
        )
        self._data_client = StockHistoricalDataClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
        )
        self._order_handlers = {
            MarketOrder:        self._submit_market_order,
            MultiLegLimitOrder: self._submit_multileg_limit_order,
        }

    # ------------------------------------------------------------------
    # ExecutionBrokerABC
    # ------------------------------------------------------------------

    def get_account_snapshot(self) -> AccountSnapshot:
        self._log_io_boundary("get_account_snapshot")
        account   = self._trading_client.get_account()
        positions = self._trading_client.get_all_positions()
        orders    = self._trading_client.get_orders()

        return AccountSnapshot(
            equity=self._safe_float(account.equity),
            cash=self._safe_float(account.cash),
            option_buying_power=self._safe_float(
                getattr(account, "options_buying_power", None)
                or getattr(account, "buying_power", 0)
            ),
            positions=[self._position_to_dict(p) for p in (positions or [])],
            open_orders=[self._order_to_dict(o) for o in (orders or [])],
        )

    def submit_order(self, order: OrderABC) -> Any:
        self._log_io_boundary("submit_order")
        return self._dispatch_by_type(
            order,
            self._order_handlers,
            error_prefix="Unsupported order type",
        )

    def cancel_order(self, order_id: str) -> None:
        self._log_io_boundary("cancel_order")
        self._trading_client.cancel_order_by_id(order_id)

    # ------------------------------------------------------------------
    # MarketDataProviderABC
    # ------------------------------------------------------------------

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        self._log_io_boundary("get_asset_quote")
        from alpaca.data.requests import StockLatestQuoteRequest
        sym = symbol.strip().upper()
        try:
            req  = StockLatestQuoteRequest(symbol_or_symbols=sym)
            data = self._data_client.get_stock_latest_quote(req)
            q    = data.get(sym)
            if q:
                bid = self._safe_float(getattr(q, "bid_price", None))
                ask = self._safe_float(getattr(q, "ask_price", None))
                mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else ask or bid
                return AssetQuote(
                    symbol=sym,
                    bid=bid or None,
                    ask=ask or None,
                    mid=mid or None,
                    timestamp_utc=getattr(q, "timestamp", None),
                )
        except Exception as exc:
            logger.warning("get_asset_quote failed for %s: %s", sym, exc)
        return AssetQuote(symbol=sym, bid=None, ask=None, mid=None, timestamp_utc=None)

    def get_latest_price(self, symbol: str) -> float:
        quote = self.get_asset_quote(symbol)
        return quote.mid or quote.ask or quote.bid or 0.0

    def get_daily_bars(self, symbol: str, lookback_days: int) -> Any:
        self._log_io_boundary("get_daily_bars")
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta, timezone
        sym   = symbol.strip().upper()
        end   = self.clock.now_utc()
        start = end - timedelta(days=lookback_days + 5)
        req   = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        return self._data_client.get_stock_bars(req)

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
        """
        Fetch option chain with quotes and greeks via two Alpaca SDK calls:

          1. TradingClient.get_option_contracts(GetOptionContractsRequest)
             Server-side filtering by expiry/type/strike. Returns contract
             metadata only — no quotes, no greeks.

          2. OptionHistoricalDataClient.get_option_chain(OptionChainRequest)
             Returns latest quote + greeks keyed by contract symbol.

        We merge: contract list defines what passes filters, data client
        provides prices. Contracts without live quote data are dropped.
        """
        self._log_io_boundary("get_option_chain")

        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionChainRequest as AlpacaOptionChainRequest

        sym = underlying.strip().upper()

        # ------------------------------------------------------------------
        # Step 1: contract list with server-side filters
        # ------------------------------------------------------------------
        contract_type = None
        if include_calls and not include_puts:
            contract_type = ContractType.CALL
        elif include_puts and not include_calls:
            contract_type = ContractType.PUT

        try:
            contracts: List[Any] = []
            page_token: Optional[str] = None
            page = 0
            while True:
                page += 1
                paged_req = GetOptionContractsRequest(
                    underlying_symbols=[sym],
                    status=AssetStatus.ACTIVE,
                    type=contract_type,
                    expiration_date_gte=expiration_date_gte,
                    expiration_date_lte=expiration_date_lte,
                    strike_price_gte=str(strike_price_gte) if strike_price_gte is not None else None,
                    strike_price_lte=str(strike_price_lte) if strike_price_lte is not None else None,
                    limit=1000,
                    page_token=page_token,
                )
                resp = self._trading_client.get_option_contracts(paged_req)
                page_contracts = resp.option_contracts or []
                contracts.extend(page_contracts)
                logger.info(
                    "Contracts page %d | sym=%s page_count=%d total=%d",
                    page, sym, len(page_contracts), len(contracts),
                )
                next_token = getattr(resp, "next_page_token", None)
                if not next_token or not page_contracts:
                    break
                page_token = str(next_token)
        except Exception as exc:
            logger.warning("get_option_contracts failed for %s: %s", sym, exc)
            return []

        if not contracts:
            logger.info(
                "No contracts returned | sym=%s expiry=[%s → %s]",
                sym, expiration_date_gte, expiration_date_lte,
            )
            return []

        contract_symbols = [str(c.symbol) for c in contracts if c.symbol]
        logger.info("Contracts fetched | sym=%s count=%d", sym, len(contract_symbols))

        # ------------------------------------------------------------------
        # Step 2: quotes + greeks from OptionHistoricalDataClient
        # get_option_chain returns { contract_sym: Snapshot } for the whole
        # underlying — we filter down to the contracts we care about.
        # ------------------------------------------------------------------
        option_data_client = OptionHistoricalDataClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
        )
        chain_data: Dict[str, Any] = {}
        try:
            chain_resp = option_data_client.get_option_chain(
                AlpacaOptionChainRequest(underlying_symbol=sym, feed=feed)
            )
            for symbol_key, snapshot in chain_resp.items():
                chain_data[str(symbol_key).upper()] = snapshot
        except Exception as exc:
            logger.warning("get_option_chain data fetch failed for %s: %s", sym, exc)

        # ------------------------------------------------------------------
        # Merge contract list + quote data
        # ------------------------------------------------------------------
        results: List[Dict[str, Any]] = []
        for contract_sym in contract_symbols:
            snapshot = chain_data.get(contract_sym.upper())
            if snapshot is None:
                continue

            quote  = getattr(snapshot, "latest_quote", None) or {}
            greeks = getattr(snapshot, "greeks",       None) or {}
            if hasattr(quote,  "__dict__"): quote  = vars(quote)
            if hasattr(greeks, "__dict__"): greeks = vars(greeks)

            results.append({
                "contract_symbol": contract_sym,
                "greeks": {
                    "delta": greeks.get("delta"),
                    "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"),
                    "vega":  greeks.get("vega"),
                },
                "latestQuote": {
                    "ap": quote.get("ask_price") or quote.get("ap"),
                    "bp": quote.get("bid_price") or quote.get("bp"),
                },
            })
            if 0 < limit <= len(results):
                break

        logger.info(
            "Option chain merged | sym=%s contracts=%d with_quotes=%d expiry=[%s → %s]",
            sym, len(contract_symbols), len(results),
            expiration_date_gte, expiration_date_lte,
        )
        return results


    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    def _submit_market_order(self, order: OrderABC) -> Any:
        if not isinstance(order, MarketOrder):
            raise TypeError(f"Expected MarketOrder, got {type(order).__name__}")
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide as AlpacaSide, TimeInForce as AlpacaTIF

        side = AlpacaSide.BUY if order.side == OrderSide.BUY else AlpacaSide.SELL
        tif  = AlpacaTIF.DAY  # always DAY for options

        req = MarketOrderRequest(
            symbol=order.symbol,
            qty=order.quantity,
            side=side,
            time_in_force=tif,
        )
        logger.info(
            "Submitting market order | symbol=%s side=%s qty=%d",
            order.symbol, side.value, order.quantity,
        )
        return self._trading_client.submit_order(req)

    def _submit_multileg_limit_order(self, order: OrderABC) -> Any:
        if not isinstance(order, MultiLegLimitOrder):
            raise TypeError(f"Expected MultiLegLimitOrder, got {type(order).__name__}")
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderClass, TimeInForce as AlpacaTIF, PositionIntent

        legs = []
        for leg in order.legs:
            side_str = "buy" if leg.side == OrderSide.BUY else "sell"
            # Determine position intent from contract + side
            # BUY = BTO (opening), SELL = STO (opening) for entries
            # For closing legs the execution policy sets position_intent
            pi = PositionIntent.BUY_TO_OPEN if leg.side == OrderSide.BUY else PositionIntent.SELL_TO_OPEN
            legs.append(OptionLegRequest(
                symbol=leg.contract.option_symbol,
                ratio_qty=leg.ratio,
                side=side_str,
                position_intent=pi,
            ))

        req = LimitOrderRequest(
            order_class=OrderClass.MLEG,
            qty=order.quantity,
            limit_price=float(order.limit_price),
            time_in_force=AlpacaTIF.DAY,
            legs=legs,
        )
        logger.info(
            "Submitting MLEG order | underlying=%s legs=%d limit=%.2f qty=%d",
            order.underlying, len(legs), float(order.limit_price), order.quantity,
        )
        return self._trading_client.submit_order(req)

    # ------------------------------------------------------------------
    # Dict converters — Alpaca SDK objects → plain dicts the app expects
    # ------------------------------------------------------------------

    @staticmethod
    def _position_to_dict(p: Any) -> Dict[str, Any]:
        def _enum_val(v: Any) -> str:
            """
            Alpaca SDK returns enum objects like AssetClass.US_OPTION.
            str(AssetClass.US_OPTION) → "AssetClass.US_OPTION" (useless).
            getattr(v, "value", str(v)) → "us_option" (what we need).
            The classifier and state machine always compare against lowercase
            plain strings, so we normalise here at the boundary.
            """
            return str(getattr(v, "value", v) or "").lower()

        # qty: Alpaca returns positive for long, negative for short options.
        # The SDK's PositionSide enum (LONG/SHORT) is on the `side` field.
        # The classifier uses qty sign directly, so we preserve the raw number.
        qty_raw = getattr(p, "qty", 0) or 0
        side    = _enum_val(getattr(p, "side", ""))
        # Enforce sign: LONG → positive, SHORT → negative
        try:
            qty_float = float(qty_raw)
        except (TypeError, ValueError):
            qty_float = 0.0
        if side == "short" and qty_float > 0:
            qty_float = -qty_float

        return {
            "symbol":            str(getattr(p, "symbol", "") or ""),
            "asset_class":       _enum_val(getattr(p, "asset_class", "")),
            "qty":               str(qty_float),
            "side":              side,
            "market_value":      str(getattr(p, "market_value", 0) or 0),
            "cost_basis":        str(getattr(p, "cost_basis", 0) or 0),
            "unrealized_pl":     str(getattr(p, "unrealized_pl", 0) or 0),
            "current_price":     str(getattr(p, "current_price", 0) or 0),
            "underlying_symbol": str(getattr(p, "underlying_symbol", "") or ""),
        }

    @staticmethod
    def _order_to_dict(o: Any) -> Dict[str, Any]:
        legs = []
        for leg in (getattr(o, "legs", None) or []):
            legs.append({
                "symbol": str(getattr(leg, "symbol", "") or ""),
                "side":   str(getattr(leg, "side", "") or ""),
            })
        return {
            "id":                str(getattr(o, "id", "") or ""),
            "symbol":            str(getattr(o, "symbol", "") or ""),
            "underlying_symbol": str(getattr(o, "underlying_symbol", "") or ""),
            "order_class":       str(getattr(o, "order_class", "") or ""),
            "status":            str(getattr(o, "status", "") or ""),
            "side":              str(getattr(o, "side", "") or ""),
            "qty":               str(getattr(o, "qty", 0) or 0),
            "legs":              legs,
        }