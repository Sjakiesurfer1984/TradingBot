from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from TradingBot.domain.types import AssetQuote


class MarketDataProviderABC(ABC):
    """
    Market data contract.

    Why this exists:
    - Keeps interface segregation: data retrieval is separate from execution.
    - Enforces strict behaviour through ABC, not Protocol.
    """

    @abstractmethod
    def get_asset_quote(self, symbol: str) -> AssetQuote:
        raise NotImplementedError

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_daily_bars(self, symbol: str, lookback_days: int) -> Any:
        raise NotImplementedError
    # Note: Flexibility belongs in the domain model, not in the broker interface. If you put flexibility in the broker interface, 
    # you force every caller to reason about it. So we do NOT implement something like def get_ohlcv(self, symbol: str, bar_steps: str, lookback_time: int)
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


# Transitional alias to keep existing imports working, if any.
MarketDataProvider = MarketDataProviderABC
