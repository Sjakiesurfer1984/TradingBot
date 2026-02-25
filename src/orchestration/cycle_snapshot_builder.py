from __future__ import annotations

from dataclasses import dataclass
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

    Composed from BrokerABC + ClockABC — no inheritance, no ABC of its own.
    One class, two clock variants:
      clock=LiveClock()      → production
      clock=BacktestClock()  → backtest (set by BacktestRunner before each cycle)

    Build sequence:
      1. broker.get_account_snapshot() — one call, returns typed AccountSnapshot
         (equity, cash, buying_power, positions, open_orders)
      2. broker.get_asset_quote(sym) — once per symbol in universe
      3. broker.get_option_chain(req) — once per OptionChainRequest, cached per cycle
    """

    broker: BrokerABC
    clock:  ClockABC = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.clock is None:
            object.__setattr__(self, "clock", LiveClock())

    def build_snapshot(
        self,
        *,
        universe:             List[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshot:
        as_of = self.clock.now_utc()

        # One call — typed AccountSnapshot covers everything about the account.
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

        chains: Dict[Tuple[Symbol, str], List[Any]] = {}
        for req in option_chain_requests:
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

        return CycleSnapshot(
            as_of_utc=as_of,
            account=account,
            asset_quotes=quotes,
            option_chains=chains,
            signals=SignalSnapshot.empty(as_of_utc=as_of),
        )