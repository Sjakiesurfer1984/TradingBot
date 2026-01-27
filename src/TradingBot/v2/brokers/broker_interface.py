# src/TradingBot/v2/brokers/broker_interface.py
from __future__ import annotations

from abc import abstractmethod
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Protocol

from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
from TradingBot.v2.brokers.execution_broker import ExecutionBroker
from TradingBot.v2.brokers.market_data_provider import MarketDataProvider
from TradingBot.v2.domain.types import AssetQuote


class BrokerInterface(MarketDataProvider, ExecutionBroker, Protocol):
    """
    Runtime contract for broker adapters.

    Notes
    - This is a Protocol: it defines required methods only.
    - Do not implement shared logic here.
    """

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_option_buying_power(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_asset_quote(self, symbol: str) -> AssetQuote:
        raise NotImplementedError

    @abstractmethod
    def get_option_chain(
        self,
        underlying: str,
        *,
        include_calls: bool = True,
        include_puts: bool = False,
        feed: str = "indicative",
        max_age_seconds: int = 5,
        limit: int = 0,
        strike_price_gte: Optional[float] = None,
        strike_price_lte: Optional[float] = None,
        expiration_date: Optional[date] = None,
        expiration_date_gte: Optional[date] = None,
        expiration_date_lte: Optional[date] = None,
        root_symbol: Optional[str] = None,
        updated_since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    def _log_io_boundary(self, method_name: str) -> None:
        """
        Optional but recommended to enforce in type checking.

        This is declared here for typing only.
        Implementation must live in a real base class (inside broker_base.py).
        """
        raise NotImplementedError
