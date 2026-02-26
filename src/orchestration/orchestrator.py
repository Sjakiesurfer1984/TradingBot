from __future__ import annotations

from dataclasses import dataclass
from typing import List

from src.brokers.interfaces import BrokerABC
from src.domain.intents import TradeIntent
from src.domain.orders import OrderABC
from src.domain.types import OptionChainRequest, Symbol
from src.execution.execution_policy_interface import ExecutionPolicyABC
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.risk.risk_engine import RiskEngine
from src.signals.signal_pipeline import SignalPipelineABC
from src.strategies.interfaces import OptionChainConsumerABC, StrategyABC
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("TradingOrchestrator")


@dataclass(frozen=True)
class CycleRunResult:
    orders_submitted:  int
    intents_generated: int
    intents_approved:  int
    intents_rejected:  int
    has_pending_orders: bool = False  # True if open orders exist after this cycle


@dataclass
class TradingOrchestrator:
    """
    Fixed cycle skeleton — all steps delegated to composed parts.

    Depends on BrokerABC, StrategyABC[], RiskEngine, ExecutionPolicyABC,
    CycleSnapshotBuilder, SignalPipelineABC. No OrchestratorABC above it —
    Scheduler holds TradingOrchestrator directly. One orchestrator exists;
    the ABC earned nothing.

    Cycle sequence:
      1. Collect universe (symbols declared by all strategies)
      2. Pre-snapshot — account + quotes, no chains yet
      3. Signal computation on pre-snapshot
      4. Collect chain requests from OptionChainConsumer strategies
      5. Full snapshot — adds option chains
      6. generate_intents from each strategy
      7. RiskEngine.evaluate — gates then sizes
      8. ExecutionPolicy.to_orders — semantic payloads → broker orders
      9. broker.submit_order — skipped if dry_run
    """

    broker:           BrokerABC
    strategies:       List[StrategyABC]
    risk_engine:      RiskEngine
    execution_policy: ExecutionPolicyABC
    snapshot_builder: CycleSnapshotBuilder
    signal_pipeline:  SignalPipelineABC
    dry_run:          bool = False

    def run_cycle(self) -> CycleRunResult:
        with log_scope("orchestrator.run_cycle", logger):
            universe = self._collect_universe()

            # One account + quote fetch for the whole cycle.
            snapshot = self.snapshot_builder.build_snapshot(universe=universe)
            signals  = self.signal_pipeline.compute(snapshot)
            snapshot = snapshot.with_signals(signals)

            # Determine chain requests from the pre-snapshot, then attach
            # chains without re-fetching account or quotes.
            chain_requests = self._collect_chain_requests(snapshot)
            snapshot       = self.snapshot_builder.add_option_chains(
                snapshot, chain_requests,
            )

            intents   = self._collect_intents(snapshot)
            decisions = self.risk_engine.evaluate(snapshot=snapshot, intents=intents)
            orders    = self.execution_policy.to_orders(approvals=decisions.approved)
            submitted = self._submit_orders(orders)

        pending = len(snapshot.account.open_orders) > 0 or submitted > 0
        return CycleRunResult(
            orders_submitted=submitted,
            intents_generated=len(intents),
            intents_approved=len(decisions.approved),
            intents_rejected=len(decisions.rejected),
            has_pending_orders=pending,
        )

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _collect_universe(self) -> List[Symbol]:
        symbols: List[Symbol] = []
        for strategy in self.strategies:
            for sym in strategy.get_symbols():
                if sym not in symbols:
                    symbols.append(sym)
        return symbols

    def _collect_chain_requests(self, snapshot: CycleSnapshot) -> List[OptionChainRequest]:
        requests: List[OptionChainRequest] = []
        for strategy in self.strategies:
            if isinstance(strategy, OptionChainConsumerABC):
                requests.extend(strategy.get_option_chain_requests(snapshot))
        return requests

    def _collect_intents(self, snapshot: CycleSnapshot) -> List[TradeIntent]:
        intents: List[TradeIntent] = []
        for strategy in self.strategies:
            intents.extend(strategy.generate_intents(snapshot))
        return intents

    def _submit_orders(self, orders: List[OrderABC]) -> int:
        if not orders:
            return 0
        if self.dry_run:
            logger.info("DRY RUN — skipping %d orders", len(orders))
            return 0
        submitted = 0
        for order in orders:
            try:
                self.broker.submit_order(order)
                submitted += 1
                logger.info("Order submitted | type=%s", type(order).__name__)
            except Exception:
                logger.exception("Order submission failed | type=%s", type(order).__name__)
        return submitted