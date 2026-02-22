from __future__ import annotations

from dataclasses import dataclass
from typing import List

from src.brokers.interfaces import BrokerABC
from src.domain.intents import TradeIntent
from src.domain.orders import OrderABC
from src.domain.types import OptionChainRequest, Symbol
from src.execution.execution_policy_interface import ExecutionPolicyABC
from src.orchestration.cycle_snapshot import CycleSnapshotABC
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilderABC
from src.orchestration.orchestrator_interface import CycleRunResult, OrchestratorABC
from src.risk.risk_engine import RiskEngine
from src.signals.signal_pipeline import SignalPipelineABC
from src.strategies.interfaces import OptionChainConsumerABC, StrategyABC
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("TradingOrchestrator")


@dataclass
class TradingOrchestrator(OrchestratorABC):
    broker:           BrokerABC
    strategies:       List[StrategyABC]
    risk_engine:      RiskEngine
    execution_policy: ExecutionPolicyABC
    snapshot_builder: CycleSnapshotBuilderABC
    signal_pipeline:  SignalPipelineABC
    dry_run:          bool = False

    # ------------------------------------------------------------------
    # Template Method: fixed skeleton, swappable steps
    # ------------------------------------------------------------------

    def run_cycle(self) -> CycleRunResult:
        with log_scope("orchestrator.run_cycle", logger):
            universe       = self._collect_universe()
            pre_snapshot   = self.snapshot_builder.build_snapshot(universe=universe, option_chain_requests=[])
            signals        = self.signal_pipeline.compute(pre_snapshot)
            pre_snapshot   = pre_snapshot.with_signals(signals)
            chain_requests = self._collect_chain_requests(pre_snapshot)
            snapshot       = self.snapshot_builder.build_snapshot(universe=universe, option_chain_requests=chain_requests)
            snapshot       = snapshot.with_signals(signals)
            intents        = self._collect_intents(snapshot)
            decisions      = self.risk_engine.evaluate(snapshot=snapshot, intents=intents)
            orders         = self.execution_policy.to_orders(approvals=decisions.approved)
            submitted      = self._submit_orders(orders)

        return CycleRunResult(
            orders_submitted=submitted,
            intents_generated=len(intents),
            intents_approved=len(decisions.approved),
            intents_rejected=len(decisions.rejected),
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

    def _collect_chain_requests(self, snapshot: CycleSnapshotABC) -> List[OptionChainRequest]:
        requests: List[OptionChainRequest] = []
        for strategy in self.strategies:
            if isinstance(strategy, OptionChainConsumerABC):
                requests.extend(strategy.get_option_chain_requests(snapshot))
        return requests

    def _collect_intents(self, snapshot: CycleSnapshotABC) -> List[TradeIntent]:
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
