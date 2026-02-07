from __future__ import annotations

from typing import Protocol, Any


class MarketDataProvider(Protocol):
    """
    Read-only market data access.
    Keep it minimal, add methods only when a strategy needs them.
    """

    def get_latest_price(self, symbol: str) -> float:
        ...

    def get_ohlcv_daily(self, symbol: str, lookback_days: int) -> Any:
        """
        Return daily OHLCV history in whatever structure you already use today.
        Later we can standardise to a DataFrame or a domain type.
        """
        ...
