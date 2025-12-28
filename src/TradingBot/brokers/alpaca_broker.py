# TradingBot/brokers/alpaca_broker.py
#
# This module provides the Alpaca-specific implementation of BrokerInterface.
#
# Core design principle:
# ----------------------
# Strategies must NEVER import or call the Alpaca SDK directly.
# Strategies talk ONLY to BrokerInterface.
#
# Why we do this:
# - We keep strategy logic portable (swap Alpaca out for IBKR later without rewriting strategies)
# - We keep API-specific quirks contained in one place
# - We can unit test strategy logic without the real Alpaca API
#
# This file is an ADAPTER:
# - Our internal domain objects (MultiLegLimitOrder, OptionLeg, etc.) -> Alpaca SDK requests
# - Alpaca SDK responses -> plain Python dicts and pandas DataFrames

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import math
import pandas as pd

from TradingBot.brokers.broker_interface import BrokerInterface
from TradingBot.domain.orders import MultiLegLimitOrder, OrderSide
from TradingBot.logger import setup_logger

# Alpaca SDK imports (Software Development Kit)
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient, StockLatestTradeRequest
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import TimeInForce as AlpacaTimeInForce
from alpaca.trading.requests import GetOptionContractsRequest, GetOrdersRequest
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

logger = setup_logger("AlpacaBroker")


# ---------------------------------------------------------------------------
# Account service
# ---------------------------------------------------------------------------

@dataclass
class AlpacaAccountService:
    """
    Provides access to Alpaca account-level information.

    Responsibility:
    - Fetch account balances and buying power (cash, equity, margin fields)
    - Fetch current open positions

    What it deliberately does NOT do:
    - No strategy logic
    - No sizing rules
    - No trade decisions
    """
    trading_client: TradingClient

    def get_account_info(self) -> Dict[str, Any]:
        """
        Retrieve full account information from Alpaca.

        Returns:
            Dict[str, Any]:
                A plain dictionary containing balances, buying power, margin status, etc.

        Reasoning:
            We return a dict, rather than the Alpaca model object, to ensure that
            our higher-level code does not depend on Alpaca-specific classes.
        """
        logger.debug("Fetching account info from Alpaca.")
        return self.trading_client.get_account().model_dump()

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieve all open positions.

        Returns:
            List[Dict[str, Any]]:
                Each position returned as a plain dictionary.

        Reasoning:
            Strategies and BotState should not depend on Alpaca SDK model classes.
            We normalise everything to plain Python objects here.
        """
        logger.debug("Fetching positions from Alpaca.")
        positions = self.trading_client.get_all_positions()
        return [pos.model_dump() for pos in positions]


# ---------------------------------------------------------------------------
# Market data service
# ---------------------------------------------------------------------------

@dataclass
class AlpacaMarketDataService:
    """
    Provides market data access via Alpaca.

    Scope:
    - Latest underlying prices (stocks/ETFs)
    - Option chains including Greeks and bid/ask quotes

    What it deliberately does NOT do:
    - No selection by delta (that is a strategy decision)
    - No selection by DTE (that is a strategy decision)
    - No sizing rules (that is a strategy/risk decision)

    In other words:
    - This layer only fetches raw data.
    - Higher layers decide what to do with it.
    """
    stock_data_client: StockHistoricalDataClient
    option_data_client: OptionHistoricalDataClient
    trading_client: TradingClient

    def get_asset_price(self, symbol: str) -> float:
        """
        Fetch the most recent trade price for an underlying asset.

        Args:
            symbol (str): Underlying ticker, e.g. "SPY".

        Returns:
            float: Latest traded price.

        Reasoning:
            Strategies need an underlying price to compute:
            - strike ranges (e.g. 0.9x to 1.1x)
            - moneyness comparisons
        """
        logger.debug(f"Fetching latest trade price for {symbol} from Alpaca.")
        request: StockLatestTradeRequest = StockLatestTradeRequest(symbol_or_symbols=[symbol])
        response: Dict[str, Any] = self.stock_data_client.get_stock_latest_trade(request)
        latest_trade: Any = response[symbol]
        return float(latest_trade.price)

    def get_option_chain(
        self,
        symbol: str,
        tipo: str,
        strike: Tuple[float, float],
        expiration: Tuple[date, date],
    ) -> pd.DataFrame:
        """
        Retrieve an option chain filtered by strike and expiration bounds.

        Args:
            symbol (str): Underlying ticker (e.g. "SPY").
            tipo (str): Option type, typically "call" or "put".
            strike (Tuple[float, float]): (min_strike, max_strike).
            expiration (Tuple[date, date]): (min_expiration_date, max_expiration_date).

        Returns:
            pd.DataFrame:
                A flat table with one row per option contract, including:
                - option_symbol (str)
                - strike_price (float)
                - expiration_date (date)
                - delta, gamma, vega, theta, rho (float | None)
                - ask_price, bid_price (float | None)

        Important behavioural guarantee:
            This method does NOT filter by delta or pick a "best" option.
            It only fetches the available contracts and decorates them with
            snapshots (Greeks and quotes).

        Reasoning:
            Fetching options is the broker's job.
            Selecting an option is the strategy's job.
        """
        logger.debug(f"Fetching option chain with Greeks for {symbol} from Alpaca.")

        min_strike_val, max_strike_val = strike
        min_expiration_date, max_expiration_date = expiration

        # Alpaca expects strike bounds as strings.
        # We conservatively floor the minimum strike so we do not exclude nearby strikes.
        min_strike_str: str = str(math.floor(min_strike_val))
        max_strike_str: str = str(round(max_strike_val, 2))

        option_contracts: List[Any] = []

        request: GetOptionContractsRequest = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            expiration_date=None,
            expiration_date_gte=min_expiration_date,
            expiration_date_lte=max_expiration_date,
            strike_price_gte=min_strike_str,
            strike_price_lte=max_strike_str,
            type=tipo,
            limit=100,
        )

        # Alpaca paginates option contract results.
        # We keep calling until next_page_token is None.
        while True:
            response: Any = self.trading_client.get_option_contracts(request)
            option_contracts.extend(response.option_contracts)
            if response.next_page_token is None:
                break
            request.page_token = response.next_page_token

        if not option_contracts:
            logger.warning(f"No option contracts found for {symbol}.")
            return pd.DataFrame()

        # Snapshot calls are limited by request size, so we chunk into groups of 100 symbols.
        option_symbols: List[str] = [c.symbol for c in option_contracts]
        symbol_chunks: List[List[str]] = [
            option_symbols[i:i + 100] for i in range(0, len(option_symbols), 100)
        ]

        snapshots: Dict[str, Any] = {}

        for chunk in symbol_chunks:
            snapshot_request: OptionSnapshotRequest = OptionSnapshotRequest(symbol_or_symbols=chunk)
            response_snapshots: Dict[str, Any] = self.option_data_client.get_option_snapshot(snapshot_request)
            snapshots.update(response_snapshots)

        # Normalise the Alpaca contract + snapshot objects into a flat, serialisable structure.
        data: List[Dict[str, Any]] = []

        for contract in option_contracts:
            # Some Alpaca contract objects may expose expiration under different attribute names.
            exp_date: Optional[date] = (
                getattr(contract, "expiration_date", None)
                or getattr(contract, "expiration", None)
            )

            if exp_date is None:
                logger.warning(f"Contract {contract.symbol} is missing an expiration field.")
                continue

            snapshot: Any = snapshots.get(contract.symbol)
            greeks: Any = getattr(snapshot, "greeks", None) if snapshot else None
            latest_quote: Any = getattr(snapshot, "latest_quote", None) if snapshot else None

            # We store None if a field is missing.
            # This avoids inventing values and lets the strategy decide how to handle missing data.
            data.append(
                {
                    "option_symbol": contract.symbol,
                    "strike_price": float(contract.strike_price),
                    "expiration_date": exp_date,
                    "delta": float(getattr(greeks, "delta")) if greeks and getattr(greeks, "delta", None) is not None else None,
                    "gamma": float(getattr(greeks, "gamma")) if greeks and getattr(greeks, "gamma", None) is not None else None,
                    "vega": float(getattr(greeks, "vega")) if greeks and getattr(greeks, "vega", None) is not None else None,
                    "theta": float(getattr(greeks, "theta")) if greeks and getattr(greeks, "theta", None) is not None else None,
                    "rho": float(getattr(greeks, "rho")) if greeks and getattr(greeks, "rho", None) is not None else None,
                    "ask_price": float(getattr(latest_quote, "ask_price")) if latest_quote and getattr(latest_quote, "ask_price", None) is not None else None,
                    "bid_price": float(getattr(latest_quote, "bid_price")) if latest_quote and getattr(latest_quote, "bid_price", None) is not None else None,
                }
            )

        return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Order service
# ---------------------------------------------------------------------------

@dataclass
class AlpacaOrderService:
    """
    Provides read-only access to Alpaca order state.

    Scope:
    - Retrieve open orders

    What it deliberately does NOT do:
    - It does not submit orders
    - It does not cancel orders
    """
    trading_client: TradingClient

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Retrieve all currently open (unfilled) orders.

        Returns:
            List[Dict[str, Any]]:
                A list of order dictionaries.

        Reasoning:
            Strategy-level deduplication depends on a simple "tell me what is open" call.
            We keep this read-only and normalise the return type to dicts.
        """
        logger.debug("Fetching open orders from Alpaca.")
        orders: Any = self.trading_client.get_orders(GetOrdersRequest(status="open"))
        return [o.model_dump() if hasattr(o, "model_dump") else dict(o) for o in orders]


# ---------------------------------------------------------------------------
# Alpaca broker adapter
# ---------------------------------------------------------------------------

@dataclass
class AlpacaBroker(BrokerInterface):
    """
    Concrete BrokerInterface implementation for Alpaca.

    Responsibilities:
    - Construct Alpaca SDK clients
    - Provide account info, positions, prices, option chains, and open orders
    - Translate internal order requests into Alpaca SDK order requests

    Key point:
    - Strategies do not need to know that Alpaca exists.
    - They just call BrokerInterface methods.
    """
    api_key: str
    api_secret: str
    paper: bool

    trading_client: TradingClient = field(init=False)
    stock_data_client: StockHistoricalDataClient = field(init=False)
    option_data_client: OptionHistoricalDataClient = field(init=False)

    account_service: AlpacaAccountService = field(init=False)
    market_data_service: AlpacaMarketDataService = field(init=False)
    order_service: AlpacaOrderService = field(init=False)

    def __post_init__(self) -> None:
        """
        Initialise Alpaca SDK clients and wrap them inside smaller services.

        Reasoning:
            Instead of one giant class that does everything, we split responsibilities:
            - account_service: account balances and positions
            - market_data_service: prices and option chains
            - order_service: reading orders

            AlpacaBroker then becomes the clean "front door" implementing BrokerInterface.
        """
        logger.debug("Initialising AlpacaBroker clients.")

        self.trading_client = TradingClient(self.api_key, self.api_secret, paper=self.paper)
        self.stock_data_client = StockHistoricalDataClient(self.api_key, self.api_secret)
        self.option_data_client = OptionHistoricalDataClient(self.api_key, self.api_secret)

        self.account_service = AlpacaAccountService(self.trading_client)
        self.market_data_service = AlpacaMarketDataService(
            self.stock_data_client,
            self.option_data_client,
            self.trading_client,
        )
        self.order_service = AlpacaOrderService(self.trading_client)

        account: Any = self.trading_client.get_account()
        logger.debug(f"Connected to Alpaca. Account status: {account.status}")

    # ---------------------------------------------------------------------
    # BrokerInterface methods (thin wrappers around the internal services)
    # ---------------------------------------------------------------------

    def get_account_info(self) -> Dict[str, Any]:
        """
        Return normalised account information.
        """
        return self.account_service.get_account_info()

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Return normalised positions list.
        """
        return self.account_service.get_positions()

    def get_asset_price(self, symbol: str) -> float:
        """
        Return latest underlying price.
        """
        return self.market_data_service.get_asset_price(symbol)

    def get_option_chain(
        self,
        symbol: str,
        tipo: str,
        strike: Tuple[float, float],
        expiration: Tuple[date, date],
    ) -> pd.DataFrame:
        """
        Return a normalised option chain DataFrame.
        """
        return self.market_data_service.get_option_chain(
            symbol=symbol,
            tipo=tipo,
            strike=strike,
            expiration=expiration,
        )

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return all open orders as plain dictionaries.
        """
        return self.order_service.get_open_orders()

    def get_buying_power(self, buying_power_type: str) -> float:
        """
        Retrieve a specific buying power field from account info.

        Args:
            buying_power_type (str):
                The key name inside Alpaca's account payload, for example:
                - "options_buying_power"
                - "buying_power"
                - "cash"

        Returns:
            float: The requested buying power value, or 0.0 if missing.

        Reasoning:
            Alpaca returns many numeric values as strings.
            We convert to float here so that strategies can safely do maths.
        """
        account_info: Dict[str, Any] = self.get_account_info()
        logger.debug(f"Retrieving {buying_power_type} from account info.")
        logger.debug(f"Account info: {account_info}")

        try:
            return float(account_info.get(buying_power_type, 0.0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Unable to parse {buying_power_type} as float.") from exc

    def get_option_buying_power(self) -> float:
        """
        Convenience wrapper used by options strategies.

        Returns:
            float: Account's options buying power.
        """
        return self.get_buying_power("options_buying_power")

    def submit_order(self, order_request: MultiLegLimitOrder) -> Any:
        """
        Submit a broker-agnostic multi-leg order through Alpaca.

        Args:
            order_request (MultiLegLimitOrder):
                Our internal representation of a multi-leg limit order.

        Returns:
            Any:
                The Alpaca SDK response object (left as Any because the SDK model type
                can vary depending on SDK version).

        Reasoning:
            Strategies build an internal MultiLegLimitOrder.
            Only the broker layer knows how to translate that into Alpaca's request objects.
        """
        legs: List[OptionLegRequest] = []

        # Convert our internal legs into Alpaca's leg request objects.
        for leg in order_request.legs:
            alpaca_side: str = "buy" if leg.side == OrderSide.BUY else "sell"
            legs.append(
                OptionLegRequest(
                    symbol=leg.symbol,
                    side=alpaca_side,
                    ratio_qty=int(leg.ratio_qty),
                )
            )

        # Convert our internal TimeInForce into Alpaca's enum.
        tif: AlpacaTimeInForce = (
            AlpacaTimeInForce.DAY
            if order_request.time_in_force.value == "DAY"
            else AlpacaTimeInForce.GTC
        )

        # Create the Alpaca order request.
        # - order_class=MLEG tells Alpaca this is a multi-leg options order.
        # - client_order_id is optional but extremely useful for deduplication and debugging.
        req: LimitOrderRequest = LimitOrderRequest(
            qty=int(order_request.qty),
            order_class=AlpacaOrderClass.MLEG,
            time_in_force=tif,
            legs=legs,
            limit_price=float(order_request.limit_price),
            client_order_id=order_request.client_order_id,
        )

        # Submit the translated order to Alpaca.
        return self.trading_client.submit_order(req)
