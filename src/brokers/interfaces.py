from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.domain.orders import OrderABC
from src.domain.types import AssetQuote
from src.orchestration.cycle_snapshot import AccountSnapshot


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
    def get_account_snapshot(self) -> AccountSnapshot:
        """
        Return a typed snapshot of all account state in one call.

        Covers: equity, cash, option_buying_power, positions, open_orders.

        CycleSnapshotBuilder calls this once per cycle — no separate
        get_equity(), get_positions(), get_open_orders() calls needed.
        All downstream code reads typed AccountSnapshot fields.
        """
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: OrderABC) -> Any:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError


class BrokerABC(MarketDataProviderABC, ExecutionBrokerABC, ABC):
    """Full broker — composition of market data + execution."""