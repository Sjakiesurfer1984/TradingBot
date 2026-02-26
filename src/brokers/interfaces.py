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

        Called only by PositionReconcilerABC — not by strategies or the orchestrator
        directly. Keeps the mutation path narrow and testable in isolation.
        """
        raise NotImplementedError

    @abstractmethod
    def get_db_symbols(self, underlying: str) -> List[str]:
        """
        Return all OSI symbols currently in the positions table for this underlying.

        Called only by PositionReconcilerABC to compute the diff against live
        broker positions. Kept separate from get_position_roles() so the
        reconciler can see all symbols, including those with role=''.
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