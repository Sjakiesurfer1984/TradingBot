from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Tuple

from src.domain.signals import SignalSnapshot
from src.domain.types import AssetQuote, Symbol


@dataclass(frozen=True)
class AccountSnapshot:
    """
    Typed account state — fetched once per cycle via broker.get_account_snapshot().
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
    with_* methods return new instances (immutable value object pattern).

    position_roles: {underlying → {osi_symbol → 'leap'|'near'}}
      Populated by the orchestrator from TradeDatabase before strategies run.
      Strategies read this instead of depending on the DB directly.
      Empty dict = no roles recorded yet (new deployment or pre-role positions).
      Classifier falls back to DTE heuristics for symbols not in this dict.
    """
    as_of_utc:       datetime
    account:         AccountSnapshot
    asset_quotes:    Dict[Symbol, AssetQuote]
    option_chains:   Dict[Tuple[Symbol, str], List[Dict[str, Any]]]
    signals:         SignalSnapshot
    position_roles:  Dict[str, Dict[str, str]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def equity(self) -> float:
        return self.account.equity

    def option_buying_power(self) -> float:
        return self.account.option_buying_power

    def positions(self) -> List[Dict[str, Any]]:
        return list(self.account.positions)

    def open_orders(self) -> List[Dict[str, Any]]:
        return list(self.account.open_orders)

    def get_position_roles(self, underlying: str) -> Dict[str, str]:
        """Return {osi_symbol: role} for the given underlying. Empty dict if unknown."""
        return self.position_roles.get(underlying.strip().upper(), {})

    # ------------------------------------------------------------------
    # Immutable update — returns new snapshot, original unchanged
    # ------------------------------------------------------------------

    def with_signals(self, signals: SignalSnapshot) -> "CycleSnapshot":
        return replace(self, signals=signals)

    def with_position_roles(
        self, position_roles: Dict[str, Dict[str, str]]
    ) -> "CycleSnapshot":
        return replace(self, position_roles=position_roles)