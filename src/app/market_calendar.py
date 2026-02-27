from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.domain.market_calendar import MarketCalendarABC
from src.utilities.logger import setup_logger

logger = setup_logger("MarketCalendar")


@dataclass(frozen=True)
class AlpacaMarketCalendar(MarketCalendarABC):
    """
    Queries Alpaca's /v2/clock endpoint for real-time market status.

    One lightweight API call per scheduler check — no market data involved.
    If the call fails, assumes closed (safe default: never submits orders
    into an unknown market state).

    GoF Strategy: the calendar is a swappable behaviour injected into the
    Scheduler — the scheduler never knows which implementation it holds.
    """

    _trading_client: Any  # alpaca.trading.client.TradingClient

    def is_open(self) -> bool:
        try:
            clock = self._trading_client.get_clock()
            open_ = bool(clock.is_open)
            logger.debug(
                "Market clock | is_open=%s next_open=%s next_close=%s",
                open_, clock.next_open, clock.next_close,
            )
            return open_
        except Exception:
            logger.exception("Failed to fetch market clock — assuming closed")
            return False

    def seconds_until_open(self) -> float:
        try:
            clock     = self._trading_client.get_clock()
            if clock.is_open:
                return 0.0
            now_utc   = datetime.now(timezone.utc)
            next_open = clock.next_open
            if next_open.tzinfo is None:
                next_open = next_open.replace(tzinfo=timezone.utc)
            return max(0.0, (next_open - now_utc).total_seconds())
        except Exception:
            logger.exception(
                "Failed to fetch market clock for next open — "
                "defaulting to 60s retry"
            )
            return 60.0


class AlwaysOpenMarketCalendar(MarketCalendarABC):
    """
    No-op calendar — trading is always permitted.

    Used for:
      - Backtests (time is controlled by BacktestClock, not wall time)
      - Unit tests
      - Any future 24/7 asset class (crypto, forex)
    """

    def is_open(self) -> bool:
        return True

    def seconds_until_open(self) -> float:
        return 0.0