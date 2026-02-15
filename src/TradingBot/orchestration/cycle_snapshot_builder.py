from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

from TradingBot.brokers.broker_interface import ExecutionBrokerABC, MarketDataProviderABC
from TradingBot.domain.signals import SignalSnapshot
from TradingBot.domain.types import AssetQuote, OptionChainRequest, Symbol, normalise_symbol
from TradingBot.orchestration.cycle_snapshot import CycleSnapshot, CycleSnapshotABC
from TradingBot.signals.default_signal_pipeline import DefaultSignalPipeline
from TradingBot.signals.signal_pipeline_interface import SignalPipelineABC
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("CycleSnapshotBuilder")


class CycleSnapshotBuilderABC(ABC):
    @abstractmethod
    def build_snapshot(
        self,
        *,
        execution_broker: ExecutionBrokerABC,
        market_data: MarketDataProviderABC,
        universe: Set[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshotABC:
        raise NotImplementedError


@dataclass
class DefaultCycleSnapshotBuilder(CycleSnapshotBuilderABC):
    signal_pipeline: SignalPipelineABC = field(default_factory=DefaultSignalPipeline)

    def build_snapshot(
        self,
        *,
        execution_broker: ExecutionBrokerABC,
        market_data: MarketDataProviderABC,
        universe: Set[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshotABC:
        as_of_utc: datetime = datetime.now(timezone.utc)

        with log_scope(
            "snapshot.build_snapshot",
            logger,
            extra=f"universe={len(universe)} option_chain_requests={len(option_chain_requests)}",
        ):
            option_buying_power: float = float(execution_broker.get_option_buying_power())
            equity: float = float(execution_broker.get_equity())
            positions: List[Dict[str, Any]] = list(execution_broker.get_positions())
            open_orders: List[Dict[str, Any]] = list(execution_broker.get_open_orders())

            asset_quotes: Dict[Symbol, AssetQuote] = {}
            for sym in sorted(universe, key=lambda s: str(s)):
                asset_quotes[sym] = market_data.get_asset_quote(sym)

            option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]] = {}
            for req in option_chain_requests:
                underlying_sym: Symbol = normalise_symbol(str(req.underlying).strip().upper())
                chain: List[Dict[str, Any]] = market_data.get_option_chain(req)
                option_chains[(underlying_sym, str(req.request_id))] = chain

            snapshot: CycleSnapshot = CycleSnapshot(
                _as_of_utc=as_of_utc,
                _option_buying_power=option_buying_power,
                _equity=equity,
                _positions=positions,
                _open_orders=open_orders,
                _asset_quotes=asset_quotes,
                _option_chains=option_chains,
                _signals=SignalSnapshot.empty(as_of_utc),
            )

            signals: SignalSnapshot = self.signal_pipeline.build(snapshot)
            return snapshot.with_signals(signals)
