from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from src.brokers.interfaces import BrokerABC
from src.domain.signals import SignalSnapshot
from src.domain.types import AssetQuote, OptionChainRequest, Symbol
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.utilities.logger import setup_logger

logger = setup_logger("CycleSnapshotBuilder")


class CycleSnapshotBuilderABC(ABC):
    @abstractmethod
    def build_snapshot(
        self,
        *,
        universe: List[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshot:
        raise NotImplementedError


@dataclass(frozen=True)
class CycleSnapshotBuilder(CycleSnapshotBuilderABC):
    broker: BrokerABC

    def build_snapshot(
        self,
        *,
        universe: List[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshot:
        as_of       = datetime.now(timezone.utc)
        equity      = float(self.broker.get_equity())
        opt_bp      = float(self.broker.get_option_buying_power())
        positions   = list(self.broker.get_positions())
        open_orders = list(self.broker.get_open_orders())

        quotes: Dict[Symbol, AssetQuote] = {
            sym: self.broker.get_asset_quote(str(sym))
            for sym in universe
        }

        chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]] = {}
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
            _as_of_utc=as_of,
            _equity=equity,
            _option_buying_power=opt_bp,
            _positions=positions,
            _open_orders=open_orders,
            _asset_quotes=quotes,
            _option_chains=chains,
            _signals=SignalSnapshot.empty(as_of_utc=as_of),
        )
