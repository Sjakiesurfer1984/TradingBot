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
        Fetch option chain via Alpaca trading client snapshots endpoint.
        Filters applied client-side (server rejects most filter params).
        """
        self._log_io_boundary("get_option_chain")
        from src.risk.pmcc_sizer import parse_osi

        sym = underlying.strip().upper()
        try:
            # get_option_contracts or get_options_snapshots depending on SDK version
            snapshots = self._trading_client.get_option_contracts(
                underlying_symbols=[sym],
                feed=feed,
            )
        except AttributeError:
            # Older SDK — fall back to requests-based approach
            logger.warning("get_option_contracts not available — chain will be empty")
            return []
        except Exception as exc:
            logger.warning("Option chain fetch failed for %s: %s", sym, exc)
            return []

        results: List[Dict[str, Any]] = []
        for contract in (snapshots or []):
            contract_sym = str(getattr(contract, "symbol", "") or "")
            if not contract_sym:
                continue
            try:
                parsed = parse_osi(contract_sym)
            except ValueError:
                continue

            exp    = parsed.expiry.date()
            strike = float(parsed.strike)
            right  = parsed.right   # "C" or "P"

            if not include_calls and right == "C":
                continue
            if not include_puts and right == "P":
                continue
            if expiration_date     is not None and exp    != expiration_date:     continue
            if expiration_date_gte is not None and exp     < expiration_date_gte: continue
            if expiration_date_lte is not None and exp     > expiration_date_lte: continue
            if strike_price_gte    is not None and strike  < strike_price_gte:    continue
            if strike_price_lte    is not None and strike  > strike_price_lte:    continue

            row: Dict[str, Any] = {"contract_symbol": contract_sym}
            # Map SDK fields to the dict shape the rest of the app expects
            greeks = getattr(contract, "greeks", None) or {}
            if hasattr(greeks, "__dict__"):
                greeks = greeks.__dict__
            quote  = getattr(contract, "latest_quote", None) or {}
            if hasattr(quote, "__dict__"):
                quote = quote.__dict__

            row["greeks"]      = greeks
            row["latestQuote"] = {
                "ap": quote.get("ask_price") or quote.get("ap"),
                "bp": quote.get("bid_price") or quote.get("bp"),
            }
            results.append(row)

            if 0 < limit <= len(results):
                break

        logger.info(
            "Option chain | sym=%s total=%d expiry=[%s → %s]",
            sym, len(results), expiration_date_gte, expiration_date_lte,
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
        return {
            "symbol":           str(getattr(p, "symbol", "") or ""),
            "asset_class":      str(getattr(p, "asset_class", "") or ""),
            "qty":              str(getattr(p, "qty", 0) or 0),
            "side":             str(getattr(p, "side", "") or ""),
            "market_value":     str(getattr(p, "market_value", 0) or 0),
            "cost_basis":       str(getattr(p, "cost_basis", 0) or 0),
            "unrealized_pl":    str(getattr(p, "unrealized_pl", 0) or 0),
            "current_price":    str(getattr(p, "current_price", 0) or 0),
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