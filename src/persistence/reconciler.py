from __future__ import annotations

"""
src/persistence/reconciler.py
------------------------------
Reconciles the positions table against live broker positions each cycle.

ISP rationale:
  Reconciliation is NOT a persistence concern — it is a sync operation
  between two external systems (the local DB and the broker). Putting it
  on TradeDatabaseABC would force every DB implementor (backtest, test
  doubles) to carry a method they have no need for.

  PositionReconcilerABC is injected into the orchestrator alongside the DB.
  The backtest wires in NullPositionReconciler; live trading wires in
  LivePositionReconciler. Each implementor only does what it needs to.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Set

from src.persistence.interfaces import TradeDatabaseABC
from src.utilities.logger import setup_logger

logger = setup_logger("PositionReconciler")


class PositionReconcilerABC(ABC):
    """
    Single responsibility: prune phantom DB positions before roles are read.

    A phantom position is a DB row whose order was submitted but then
    rejected, cancelled, or timed out — the DB was written at submission
    but the broker never confirmed a fill.

    reconcile() is called once per cycle, after the AccountSnapshot is
    fetched but before get_position_roles() is called, so the state
    classifier always reads from a clean DB.
    """

    @abstractmethod
    def reconcile(self, underlying: str, broker_symbols: Set[str]) -> None:
        """
        underlying:     OSI ticker (e.g. 'SPY') — uppercase.
        broker_symbols: set of OSI symbols Alpaca currently holds for this
                        underlying, extracted from AccountSnapshot.positions.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class LivePositionReconciler(PositionReconcilerABC):
    """
    Reconciler for live trading.

    Computes the diff between what the DB thinks we hold and what the
    broker actually shows, then deletes any DB rows not in the broker set.
    """

    db: TradeDatabaseABC

    def reconcile(self, underlying: str, broker_symbols: Set[str]) -> None:
        ul          = underlying.strip().upper()
        db_symbols  = set(self.db.get_db_symbols(ul))
        phantoms    = db_symbols - broker_symbols

        if not phantoms:
            return

        logger.warning(
            "Reconcile | underlying=%s removing %d phantom position(s): %s",
            ul, len(phantoms), sorted(phantoms),
        )
        self.db.remove_positions(list(phantoms))


class NullPositionReconciler(PositionReconcilerABC):
    """
    No-op reconciler for backtests and unit tests.

    The backtest controls every fill directly — there are no order
    rejections or cancellations, so phantom positions cannot exist.
    Reconciliation would be meaningless and wasteful.
    """

    def reconcile(self, underlying: str, broker_symbols: Set[str]) -> None:
        pass