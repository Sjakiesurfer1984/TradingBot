from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set

from src.brokers.interfaces import BrokerABC
from src.domain.intents import TradeIntent
from src.domain.orders import OrderABC
from src.domain.types import OptionChainRequest, Symbol
from src.execution.execution_policy_interface import ExecutionPolicyABC
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.orchestration.cycle_snapshot_builder import CycleSnapshotBuilder
from src.persistence.interfaces import NullTradeDatabase, TradeDatabaseABC
from src.persistence.reconciler import NullPositionReconciler, PositionReconcilerABC
from src.risk.risk_engine import RiskEngine
from src.signals.signal_pipeline import SignalPipelineABC
from src.strategies.interfaces import OptionChainConsumerABC, StrategyABC
from src.utilities.logger import setup_logger
from src.utilities.logging_utils import log_scope

logger = setup_logger("TradingOrchestrator")


@dataclass(frozen=True)
class CycleRunResult:
    orders_submitted:   int
    intents_generated:  int
    intents_approved:   int
    intents_rejected:   int
    has_pending_orders: bool = False


@dataclass
class TradingOrchestrator:
    """
    Fixed cycle skeleton — all steps delegated to composed parts.

    db         — enriches the snapshot with position roles (cycle coordination).
    reconciler — prunes phantom DB rows before roles are read (ISP-compliant
                 separation: live wires LivePositionReconciler, backtest wires
                 NullPositionReconciler).

    Fill recording is the ExecutionPolicy's responsibility.

    Cycle sequence:
      1.  Collect universe
      2.  Build snapshot (account + quotes)
      3.  Reconcile DB positions against live broker positions
      4.  Enrich snapshot with position roles from DB
      5.  Compute signals
      6.  Collect chain requests from strategies
      7.  Add option chains to snapshot
      8.  generate_intents from each strategy
      9.  RiskEngine.evaluate
      10. ExecutionPolicy.to_orders (also records fills in DB)
      11. broker.submit_order — skipped if dry_run
    """

    broker:           BrokerABC
    strategies:       List[StrategyABC]
    risk_engine:      RiskEngine
    execution_policy: ExecutionPolicyABC
    snapshot_builder: CycleSnapshotBuilder
    signal_pipeline:  SignalPipelineABC
    dry_run:          bool                   = False
    db:               TradeDatabaseABC       = field(default_factory=NullTradeDatabase)
    reconciler:       PositionReconcilerABC  = field(default_factory=NullPositionReconciler)

    def run_cycle(self) -> CycleRunResult:
        with log_scope("orchestrator.run_cycle", logger):
            universe = self._collect_universe()

            snapshot = self.snapshot_builder.build_snapshot(universe=universe)
            self._reconcile(snapshot, universe)
            snapshot = self._enrich_position_roles(snapshot, universe)
            signals  = self.signal_pipeline.compute(snapshot)
            snapshot = snapshot.with_signals(signals)

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

    def _reconcile(
        self,
        snapshot: CycleSnapshot,
        universe: List[Symbol],
    ) -> None:
        """
        For each underlying, extract live broker symbols from the already-fetched
        AccountSnapshot and call the reconciler to prune any phantom DB rows.

        Zero extra broker calls — we reuse positions already in memory.
        """
        all_positions = snapshot.account.positions
        for sym in universe:
            ul = str(sym).upper()
            broker_symbols: Set[str] = {
                str(p.get("symbol", "")).strip().upper()
                for p in all_positions
                if str(p.get("symbol", "")).strip().upper().startswith(ul)
                and str(p.get("asset_class", "")).lower() == "us_option"
            }
            self.reconciler.reconcile(ul, broker_symbols)

    def _enrich_position_roles(
        self,
        snapshot: CycleSnapshot,
        universe: List[Symbol],
    ) -> CycleSnapshot:
        """
        Fetch position roles from the DB for every symbol in the universe
        and attach them to the snapshot.

        Strategies read snapshot.get_position_roles(sym) — they never
        touch the DB directly, keeping strategy concerns pure.
        """
        roles: Dict[str, Dict[str, str]] = {}
        for sym in universe:
            sym_str = str(sym).upper()
            result  = self.db.get_position_roles(sym_str)
            if result:
                roles[sym_str] = result
        return snapshot.with_position_roles(roles)

    def _collect_chain_requests(
        self, snapshot: CycleSnapshot
    ) -> List[OptionChainRequest]:
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
            except Exception:
                logger.exception("Order submission failed | order=%s", order)
        return submitted