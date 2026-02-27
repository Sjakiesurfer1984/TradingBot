from __future__ import annotations

from abc import ABC, abstractmethod


class MarketCalendarABC(ABC):
    """
    Abstraction over market trading hours.

    SRP:  knows only whether trading is permitted right now and when it
          resumes. Nothing else.
    OCP:  new schedule variants (equities, crypto 24/7, forex, test stub)
          are added by subclassing — zero changes to the scheduler or any
          other consumer.
    DIP:  all consumers (Scheduler, tests) depend on this ABC, never on a
          concrete implementation.

    Placed in src/domain/ so it can be imported by both src/app/ and
    src/brokers/ without any circular dependency risk.
    """

    @abstractmethod
    def is_open(self) -> bool:
        """Return True if trading is currently permitted."""
        raise NotImplementedError

    @abstractmethod
    def seconds_until_open(self) -> float:
        """
        Seconds until trading next becomes permitted.
        Returns 0.0 if trading is currently open.
        """
        raise NotImplementedError