from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.domain.orders import OrderABC
from src.domain.types import AssetQuote


class MarketDataProviderABC(ABC):

    @abstractmethod
    def get_asset_quote(self, symbol: str) -> AssetQuote:
        raise NotImplementedError

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_daily_bars(self, symbol: str, lookback_days: int) -> Any:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError
    

    

class ExecutionBrokerABC(ABC):

    @abstractmethod
    def get_equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    # THis is to use the individual methods to provide an entire account snapshot. Not sure yet how to deal with this. 
    def get_account_snapshot(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_option_buying_power(self) -> float:
        raise NotImplementedError
    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: OrderABC) -> Any:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError


class BrokerABC(MarketDataProviderABC, ExecutionBrokerABC, ABC):
    """Full broker — composition of market data + execution. Depend on the narrow interface where possible."""
