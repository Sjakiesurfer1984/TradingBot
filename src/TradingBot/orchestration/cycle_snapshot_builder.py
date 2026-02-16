from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Tuple

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.domain.signals import SignalSnapshot
from TradingBot.domain.types import AssetQuote, OptionChainRequest, Symbol
from TradingBot.orchestration.cycle_snapshot import CycleSnapshot


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
    """
    Builds a per-cycle snapshot by performing broker IO.

    Contract
    - This class does broker IO.
    - It does NOT compute signals. Signals are attached later by the signal pipeline.
    - Returns a CycleSnapshot compatible with src/TradingBot/orchestration/cycle_snapshot.py
    """

    broker: BrokerABC

    def build_snapshot(
        self,
        *,
        universe: List[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshot:
        as_of_utc: datetime = datetime.utcnow()

        option_buying_power: float = float(self.broker.get_option_buying_power())
        equity: float = float(self.broker.get_equity())

        positions: List[Dict[str, Any]] = list(self.broker.get_positions())
        open_orders: List[Dict[str, Any]] = list(self.broker.get_open_orders())

        asset_quotes: Dict[Symbol, AssetQuote] = {}
        for sym in universe:
            asset_quotes[sym] = self.broker.get_asset_quote(sym)

        option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]] = {}
        for req in option_chain_requests:
            chain: List[Dict[str, Any]] = self.broker.get_option_chain(
                req.underlying,
                include_calls=bool(req.include_calls),
                include_puts=bool(req.include_puts),
                expiration_date_gte=req.expiration_date_gte,
                expiration_date_lte=req.expiration_date_lte,
                feed=str(req.feed),
                limit=int(req.limit),
            )
            option_chains[(req.underlying, str(req.request_id))] = chain

        return CycleSnapshot(
            _as_of_utc=as_of_utc,
            _option_buying_power=option_buying_power,
            _equity=equity,
            _positions=positions,
            _open_orders=open_orders,
            _asset_quotes=asset_quotes,
            _option_chains=option_chains,
            _signals=SignalSnapshot.empty(as_of_utc=as_of_utc),
            
        )
