from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, List, Optional


class TradeDatabaseABC(ABC):
    """
    Interface for the trade persistence layer.

    Responsibility: store and retrieve trade data. Nothing else.

    leg_role is passed on every record_fill() call:
      'leap' — this fill opens/closes a long LEAP leg
      'near' — this fill opens/closes a short NEAR leg
      ''     — unknown or not applicable

    get_position_roles() is the primary source of truth for the state
    classifier. The classifier reads this first and falls back to DTE
    heuristics only for symbols with role=''.
    """

    @abstractmethod
    def record_fill(
        self,
        *,
        action:          str,
        symbol:          str,
        underlying:      str,
        qty:             int,
        fill_price:      float,
        fill_date:       date,
        expiry:          Optional[date],
        strike:          Optional[float],
        option_right:    Optional[str],
        cost_basis_usd:  float,
        leg_role:        str = "",
        brokerage_fee:   float = 0.0,
        usd_aud_rate:    Optional[float] = None,
        notes:           str = "",
    ) -> int:
        raise NotImplementedError

    @abstractmethod
    def get_position_roles(self, underlying: str) -> Dict[str, str]:
        """Return {osi_symbol: leg_role} for open positions where role is known."""
        raise NotImplementedError

    @abstractmethod
    def remove_positions(self, symbols: List[str]) -> None:
        """
        Delete positions by OSI symbol.
        Called only by PositionReconcilerABC.
        """
        raise NotImplementedError

    @abstractmethod
    def get_db_symbols(self, underlying: str) -> List[str]:
        """
        Return all OSI symbols in the positions table for this underlying.
        Called only by PositionReconcilerABC.
        """
        raise NotImplementedError

    @abstractmethod
    def record_chain_snapshot(
        self,
        *,
        snapshot_date: date,
        underlying:    str,
        contracts:     List[Dict[str, Any]],
    ) -> None:
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
        raise NotImplementedError

    @abstractmethod
    def get_fills(
        self,
        *,
        from_date:  Optional[date] = None,
        to_date:    Optional[date] = None,
        underlying: Optional[str]  = None,
        action:     Optional[str]  = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_equity_curve(
        self,
        *,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_chain_snapshot(
        self,
        *,
        snapshot_date: date,
        underlying:    str,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class NullTradeDatabase(TradeDatabaseABC):
    """No-op implementation for tests or when persistence is disabled."""
    def record_fill(self, **kwargs) -> int:                          return 0
    def get_position_roles(self, underlying: str) -> Dict[str, str]: return {}
    def remove_positions(self, symbols: List[str]) -> None:          pass
    def get_db_symbols(self, underlying: str) -> List[str]:          return []
    def record_chain_snapshot(self, **kwargs) -> None:               pass
    def record_equity_snapshot(self, **kwargs) -> None:              pass
    def get_fills(self, **kwargs) -> List[Dict[str, Any]]:           return []
    def get_equity_curve(self, **kwargs) -> List[Dict[str, Any]]:    return []
    def get_chain_snapshot(self, **kwargs) -> List[Dict[str, Any]]:  return []