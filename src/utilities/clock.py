from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timezone


class ClockABC(ABC):
    """
    Abstraction over the current time.

    Inject this wherever code needs to know "what time/date is it now".
    Live trading uses LiveClock (wraps datetime.now()).
    Backtesting uses BacktestClock (returns the cycle date set by the runner).

    This eliminates all date.today() / datetime.now() calls scattered through
    strategy, snapshot builder, and broker code — fixing the root cause of the
    "wrong expiry window during backtest" bug and restoring LSP compliance.

    GoF: Strategy pattern — the clock is a swappable behaviour, not a subclass.
    SOLID:
      - SRP: clock knows only about time, nothing else.
      - OCP: new clock variants (e.g. SimulatedClock for unit tests) require
             zero changes to existing code.
      - DIP: callers depend on ClockABC, not on datetime.now().
    """

    @abstractmethod
    def now_utc(self) -> datetime:
        """Return the current datetime in UTC."""
        raise NotImplementedError

    def today(self) -> date:
        """Return the current date (UTC)."""
        return self.now_utc().date()


class LiveClock(ClockABC):
    """
    Production clock. Returns the real current time.
    Stateless — safe to share across threads.
    """

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class BacktestClock(ClockABC):
    """
    Backtest clock. Returns whatever date the BacktestRunner has set.

    The runner calls set_date() at the start of each cycle.
    All components that depend on ClockABC automatically see the correct
    historical date without any subclassing or monkey-patching.

    Thread safety: backtests are single-threaded so no locking needed.
    """

    def __init__(self) -> None:
        self._date: date = date.today()

    def set_date(self, d: date) -> None:
        self._date = d

    def now_utc(self) -> datetime:
        return datetime.combine(self._date, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )