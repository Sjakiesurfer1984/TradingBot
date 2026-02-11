from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

from TradingBot.brokers.broker_interface import ExecutionBrokerABC, MarketDataProviderABC
from TradingBot.domain.types import AssetQuote, OptionChainRequest, Symbol
from TradingBot.orchestration.cycle_snapshot import CycleSnapshot, CycleSnapshotABC
from TradingBot.risk.price_policy import DefaultPriceSelectionPolicy, PriceSelectionPolicy
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


class DefaultCycleSnapshotBuilder(CycleSnapshotBuilderABC):
    def __init__(self, price_policy: PriceSelectionPolicy | None = None) -> None:
        self._price_policy: PriceSelectionPolicy = price_policy or DefaultPriceSelectionPolicy()

    def build_snapshot(
        self,
        *,
        execution_broker: ExecutionBrokerABC,
        market_data: MarketDataProviderABC,
        universe: Set[Symbol],
        option_chain_requests: List[OptionChainRequest],
    ) -> CycleSnapshotABC:
        with log_scope(
            "snapshot.build_snapshot",
            logger,
            extra=f"universe={len(universe)} option_chain_requests={len(option_chain_requests)}",
        ):
            as_of_utc: datetime = datetime.now(timezone.utc)

            _account_snapshot = execution_broker.get_account_snapshot()
            option_buying_power: float = execution_broker.get_option_buying_power()
            equity: float = execution_broker.get_equity()

            positions: List[Dict[str, Any]] = execution_broker.get_positions()
            open_orders: List[Dict[str, Any]] = execution_broker.get_open_orders()

            asset_quotes: Dict[Symbol, AssetQuote] = {}
            for sym in universe:
                asset_quotes[sym] = market_data.get_asset_quote(str(sym))

            option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]] = {}
            for req in option_chain_requests:
                underlying: str = getattr(req, "underlying")
                request_id: str = getattr(req, "request_id")

                chain: List[Dict[str, Any]] = market_data.get_option_chain(
                    underlying=underlying,
                    include_calls=getattr(req, "include_calls", True),
                    include_puts=getattr(req, "include_puts", False),
                    feed=getattr(req, "feed", "indicative"),
                    max_age_seconds=getattr(req, "max_age_seconds", 5),
                    limit=getattr(req, "limit", 0),
                    strike_price_gte=getattr(req, "strike_price_gte", None),
                    strike_price_lte=getattr(req, "strike_price_lte", None),
                    expiration_date=getattr(req, "expiration_date", None),
                    expiration_date_gte=getattr(req, "expiration_date_gte", None),
                    expiration_date_lte=getattr(req, "expiration_date_lte", None),
                    root_symbol=getattr(req, "root_symbol", None),
                    updated_since=getattr(req, "updated_since", None),
                )

                option_chains[(Symbol(underlying), request_id)] = chain

            return CycleSnapshot(
                _as_of_utc=as_of_utc,
                _option_buying_power=float(option_buying_power),
                _equity=float(equity),
                _positions=positions,
                _open_orders=open_orders,
                _asset_quotes=asset_quotes,
                _option_chains=option_chains,
                _price_policy=self._price_policy,
            )
