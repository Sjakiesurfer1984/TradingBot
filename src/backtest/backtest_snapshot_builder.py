from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from src.backtest.alpaca_historical_broker import AlpacaHistoricalBroker
from src.domain.types import OptionChainRequest, Symbol
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder


class BacktestSnapshotBuilder(CycleSnapshotBuilder):
    """
    CycleSnapshotBuilder variant for backtesting.

    The only difference from the live builder: as_of_utc is stamped with
    the broker's current backtest date instead of datetime.now().

    This matters because PmccStrategy.get_option_chain_requests() calls
    _snapshot_date(snapshot) to compute option chain expiry windows.
    If as_of_utc is real-time (2026-02-24) during a 2025-03-01 backtest
    cycle, the expiry windows are wrong and Alpaca returns no contracts.
    """

    broker: AlpacaHistoricalBroker   # narrowed type — we need _current_date

    def build_snapshot(
        self,
        *,
        universe: List[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshot:
        # Build using the parent — then stamp the correct date.
        snapshot = super().build_snapshot(
            universe=universe,
            option_chain_requests=option_chain_requests,
        )
        # Replace as_of_utc with the backtest cycle date so that
        # _snapshot_date(snapshot) returns the correct historical date.
        correct_as_of = datetime.combine(
            self.broker._current_date,
            datetime.min.time(),
        ).replace(tzinfo=timezone.utc)

        # CycleSnapshot is frozen — use dataclasses.replace
        from dataclasses import replace
        return replace(snapshot, _as_of_utc=correct_as_of)