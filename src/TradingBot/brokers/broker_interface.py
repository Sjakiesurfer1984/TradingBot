from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Set

from TradingBot.domain.orders import OrderABC
from TradingBot.domain.types import AssetQuote, Symbol


class MarketDataProviderABC(ABC):
    """
    Market data provider contract.

    Responsibilities
    - Provide prices/quotes needed for decisions.
    - Provide option chain data when strategies request it.

    No order submission methods live here.
    """

    @abstractmethod
    def get_asset_quote(self, symbol: str) -> AssetQuote:
        raise NotImplementedError

    @abstractmethod
    def get_option_chain(
        self,
        *,
        underlying: str,
        include_calls: bool,
        include_puts: bool,
        feed: str,
        max_age_seconds: int,
        limit: int,
        strike_price_gte: float | None,
        strike_price_lte: float | None,
        expiration_date: str | None,
        expiration_date_gte: str | None,
        expiration_date_lte: str | None,
        root_symbol: str | None,
        updated_since: str | None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class ExecutionBrokerABC(ABC):
    """
    Execution broker contract.

    Responsibilities
    - Provide account state needed for risk controls.
    - Submit orders and expose open orders and positions.

    No market data methods live here.
    """

    @abstractmethod
    def get_account_snapshot(self) -> Dict[str, Any]:
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
    def submit_order(self, order: OrderABC) -> Any:
        raise NotImplementedError


class BrokerABC(MarketDataProviderABC, ExecutionBrokerABC, ABC):
    """
    Convenience alias for a broker that supports both execution and market data.

    You can keep using BrokerABC as a composition type in wiring, but the UML should
    treat the two capabilities separately (ISP).
    """
