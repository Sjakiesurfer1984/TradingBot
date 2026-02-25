from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, List, Optional


class TradeDatabaseABC(ABC):
    """
    Interface for the trade persistence layer.

    All components that need to write or read trade data depend on this
    interface, not on the SQLite implementation. This means:
      - Unit tests can inject a NullTradeDatabase or InMemoryTradeDatabase.
      - A future Postgres implementation requires zero changes to callers.

    SOLID — DIP: depend on abstraction, not concretion.
    """

    # ------------------------------------------------------------------
    # Writes — called by the broker on every fill
    # ------------------------------------------------------------------

    @abstractmethod
    def record_fill(
        self,
        *,
        action:          str,          # BTO | STO | BTC | STC
        symbol:          str,          # OSI symbol
        underlying:      str,          # SPY, QQQ, etc.
        qty:             int,
        fill_price:      float,        # per-share (multiply by 100 for contract cost)
        fill_date:       date,
        expiry:          Optional[date],
        strike:          Optional[float],
        option_right:    Optional[str], # C | P
        cost_basis_usd:  float,        # total cost/credit in USD (qty * price * 100)
        brokerage_fee:   float = 0.0,
        usd_aud_rate:    Optional[float] = None,
        notes:           str = "",
    ) -> int:
        """Insert a fill record. Returns the new row id."""
        raise NotImplementedError

    @abstractmethod
    def record_chain_snapshot(
        self,
        *,
        snapshot_date: date,
        underlying:    str,
        contracts:     List[Dict[str, Any]],   # raw Alpaca snapshot rows
    ) -> None:
        """Persist a full option chain snapshot for future backtesting."""
        raise NotImplementedError

    @abstractmethod
    def record_equity_snapshot(
        self,
        *,
        snapshot_date:       date,
        equity:              float,
        cash:                float,
        option_buying_power: float,
        open_positions:      int,
    ) -> None:
        """Record daily equity/account state."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Reads — called by reporting tools
    # ------------------------------------------------------------------

    @abstractmethod
    def get_fills(
        self,
        *,
        from_date:  Optional[date] = None,
        to_date:    Optional[date] = None,
        underlying: Optional[str]  = None,
        action:     Optional[str]  = None,
    ) -> List[Dict[str, Any]]:
        """Return fill records matching the given filters."""
        raise NotImplementedError

    @abstractmethod
    def get_equity_curve(
        self,
        *,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """Return daily equity snapshots."""
        raise NotImplementedError

    @abstractmethod
    def get_chain_snapshot(
        self,
        *,
        snapshot_date: date,
        underlying:    str,
    ) -> List[Dict[str, Any]]:
        """Return stored chain contracts for a given date and underlying."""
        raise NotImplementedError


class NullTradeDatabase(TradeDatabaseABC):
    """
    No-op implementation. Accepts all writes silently, returns empty reads.
    Use in tests or when persistence is disabled.
    """

    def record_fill(self, **kwargs) -> int:                    return 0
    def record_chain_snapshot(self, **kwargs) -> None:         pass
    def record_equity_snapshot(self, **kwargs) -> None:        pass
    def get_fills(self, **kwargs) -> List[Dict[str, Any]]:     return []
    def get_equity_curve(self, **kwargs) -> List[Dict[str, Any]]: return []
    def get_chain_snapshot(self, **kwargs) -> List[Dict[str, Any]]: return []