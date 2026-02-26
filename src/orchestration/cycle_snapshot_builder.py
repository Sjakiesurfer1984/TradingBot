from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Tuple

from src.brokers.interfaces import BrokerABC
from src.domain.signals import SignalSnapshot
from src.domain.types import AssetQuote, OptionChainRequest, Symbol
from src.orchestration.cycle_snapshot import AccountSnapshot, CycleSnapshot
from src.utilities.clock import ClockABC, LiveClock
from src.utilities.logger import setup_logger

logger = setup_logger("CycleSnapshotBuilder")


@dataclass(frozen=True)
class CycleSnapshotBuilder:
    """
    Builds a CycleSnapshot from live broker data.

    Two entry points:
      build_snapshot()    — full build: account + quotes (no chains).
                            Called once per cycle for the pre-snapshot.
      add_option_chains() — extend an existing snapshot with chain data,
                            reusing the account + quote data already fetched.
                            Called once per cycle after chain requests are known.

    This split avoids the previous double-fetch: account and quote data were
    being re-fetched for the full snapshot even though they hadn't changed
    since the pre-snapshot 2 seconds earlier. Saving ~2 broker round-trips
    per cycle (~4s on SPY).
    """

    broker: BrokerABC
    clock:  ClockABC = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.clock is None:
            object.__setattr__(self, "clock", LiveClock())

    def build_snapshot(
        self,
        *,
        universe: List[Symbol],
    ) -> CycleSnapshot:
        """
        Fetch account state and asset quotes. No option chains.
        Call add_option_chains() afterwards to attach chains.
        """
        as_of   = self.clock.now_utc()
        account = self.broker.get_account_snapshot()

        logger.info(
            "Account snapshot | equity=%.2f option_buying_power=%.2f "
            "positions=%d open_orders=%d",
            account.equity,
            account.option_buying_power,
            len(account.positions),
            len(account.open_orders),
        )

        quotes: Dict[Symbol, AssetQuote] = {
            sym: self.broker.get_asset_quote(str(sym))
            for sym in universe
        }

        return CycleSnapshot(
            as_of_utc=as_of,
            account=account,
            asset_quotes=quotes,
            option_chains={},
            signals=SignalSnapshot.empty(as_of_utc=as_of),
        )

    def add_option_chains(
        self,
        snapshot:              CycleSnapshot,
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshot:
        """
        Extend snapshot with option chains. Reuses account + quote data.
        Returns a new CycleSnapshot (original is immutable).
        """
        if not option_chain_requests:
            return snapshot

        new_chains = dict(snapshot.option_chains)
        new_chains.update(self._fetch_chains(option_chain_requests))
        return replace(snapshot, option_chains=new_chains)

    def _fetch_chains(
        self,
        requests: List[OptionChainRequest],
    ) -> Dict[Tuple[Symbol, str], List[Any]]:
        chains: Dict[Tuple[Symbol, str], List[Any]] = {}
        for req in requests:
            logger.info(
                "Fetching option chain | underlying=%s id=%s expiry=[%s → %s]",
                req.underlying, req.request_id,
                req.expiration_date_gte, req.expiration_date_lte,
            )
            result = self.broker.get_option_chain(
                req.underlying,
                include_calls=req.include_calls,
                include_puts=req.include_puts,
                feed=req.feed,
                limit=req.limit,
                expiration_date_gte=req.expiration_date_gte,
                expiration_date_lte=req.expiration_date_lte,
            )
            logger.info(
                "Option chain received | underlying=%s id=%s contracts=%d",
                req.underlying, req.request_id, len(result),
            )
            chains[(Symbol(req.underlying), req.request_id)] = result
        return chains