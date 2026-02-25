from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Tuple

from src.domain.signals import SignalSnapshot
from src.domain.types import AssetQuote, Symbol


@dataclass(frozen=True)
class AccountSnapshot:
    """
    Typed account state — fetched once per cycle via broker.get_account_snapshot().

    Replaces the old Dict[str, Any] return from ExecutionBrokerABC.get_account_snapshot().
    All downstream code reads typed fields — no .get("equity") string parsing.
    """
    equity:               float
    cash:                 float
    option_buying_power:  float
    positions:            List[Dict[str, Any]]
    open_orders:          List[Dict[str, Any]]


@dataclass(frozen=True)
class CycleSnapshot:
    """
    Immutable snapshot of all data available at the start of a cycle.

    Built by CycleSnapshotBuilder — never mutated after construction.
    No ABC needed: there is one snapshot type and one builder.
    Clock injection (not subclassing) handles live vs backtest timestamps.
    """
    as_of_utc:     datetime
    account:       AccountSnapshot
    asset_quotes:  Dict[Symbol, AssetQuote]
    option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]]
    signals:       SignalSnapshot

    # ------------------------------------------------------------------
    # Convenience accessors — delegate to AccountSnapshot
    # ------------------------------------------------------------------

    def equity(self) -> float:
        return self.account.equity

    def option_buying_power(self) -> float:
        return self.account.option_buying_power

    def positions(self) -> List[Dict[str, Any]]:
        return list(self.account.positions)

    def open_orders(self) -> List[Dict[str, Any]]:
        return list(self.account.open_orders)

    def with_signals(self, signals: SignalSnapshot) -> "CycleSnapshot":
        return replace(self, signals=signals)