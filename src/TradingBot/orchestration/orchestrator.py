from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Set

from TradingBot.brokers.broker_interface import BrokerABC
from TradingBot.domain.intents import TradeIntent
from TradingBot.domain.orders import OrderABC
from TradingBot.domain.types import Symbol
from TradingBot.execution.execution_policy_interface import ExecutionPolicyABC
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.orchestration.cycle_snapshot_builder import CycleSnapshotBuilderABC
from TradingBot.orchestration.orchestrator_interface import CycleRunResult, CycleRunStatus, OrchestratorABC
from TradingBot.risk.decisions import ApprovedIntent, RiskDecision
from TradingBot.risk.risk_engine import RiskEngineABC
from TradingBot.strategies.option_chain_consumer import OptionChainConsumerABC
from TradingBot.strategies.strategy_interface import StrategyABC
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("TradingOrchestrator")


@dataclass
class TradingOrchestrator(OrchestratorABC):
    broker: BrokerABC
    strategies: List[StrategyABC]
    risk_engine: RiskEngineABC
    snapshot_builder: CycleSnapshotBuilderABC
    execution_policy: ExecutionPolicyABC
    dry_run: bool = True

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _build_cycle_snapshot(self) -> CycleSnapshotABC:
        with log_scope("orchestrator._build_cycle_snapshot", logger):
            universe: Set[Symbol] = set()
            option_chain_requests = []

            for strat in self.strategies:
                universe |= set(strat.universe())

                if isinstance(strat, OptionChainConsumerABC):
                    option_chain_requests.extend(strat.option_chain_requests())

            return self.snapshot_builder.build_snapshot(
                execution_broker=self.broker,
                market_data = self.broker,
                universe=universe,
                option_chain_requests=option_chain_requests,
            )

    def _collect_intents(self, snapshot: CycleSnapshotABC) -> List[TradeIntent]:
        with log_scope("orchestrator._collect_intents", logger):
            all_intents: List[TradeIntent] = []

            for strat in self.strategies:
                try:
                    intents = strat.generate_intents(snapshot)
                except Exception as exc:
                    logger.exception("Strategy failed | strategy_type=%s error=%s", type(strat).__name__, str(exc))
                    continue

                all_intents.extend(intents)

            return all_intents

    def _evaluate_intents(self, snapshot: CycleSnapshotABC, intents: List[TradeIntent]) -> List[RiskDecision]:
        with log_scope("orchestrator._evaluate_intents", logger, extra=f"intents={len(intents)}"):
            return self.risk_engine.evaluate(snapshot, intents)

    def _submit_orders(self, orders: List[OrderABC]) -> int:
        submitted: int = 0

        with log_scope("orchestrator._submit_orders", logger, extra=f"orders={len(orders)}"):
            for order in orders:
                try:
                    self.broker.submit_order(order)
                    submitted += 1
                except Exception as exc:
                    logger.exception("Order submit failed | order_type=%s error=%s", type(order).__name__, str(exc))

        return submitted

    def run_cycle(self) -> CycleRunResult:
        started_at_utc: datetime = self._now_utc()
        intents_count: int = 0
        decisions_count: int = 0
        submitted_count: int = 0

        try:
            with log_scope("orchestrator.run_cycle", logger):
                snapshot: CycleSnapshotABC = self._build_cycle_snapshot()
                intents: List[TradeIntent] = self._collect_intents(snapshot)
                intents_count = int(len(intents))

                decisions: List[RiskDecision] = self._evaluate_intents(snapshot, intents)
                decisions_count = int(len(decisions))

                approvals: List[ApprovedIntent] = [
                    d.approved for d in decisions if d.approved is not None
                ]

                orders: List[OrderABC] = self.execution_policy.to_orders(snapshot, approvals)

                if self.dry_run:
                    logger.info("Dry run enabled: skipping order submission | orders=%d", int(len(orders)))
                else:
                    submitted_count = self._submit_orders(orders)

            ended_at_utc: datetime = self._now_utc()
            return CycleRunResult(
                started_at_utc=started_at_utc,
                ended_at_utc=ended_at_utc,
                status=CycleRunStatus.OK,
                intents_count=intents_count,
                decisions_count=decisions_count,
                submitted_count=submitted_count,
                dry_run=bool(self.dry_run),
                error_message=None,
            )

        except Exception as exc:
            logger.exception("Cycle failed | error=%s", str(exc))
            ended_at_utc = self._now_utc()
            return CycleRunResult(
                started_at_utc=started_at_utc,
                ended_at_utc=ended_at_utc,
                status=CycleRunStatus.ERROR,
                intents_count=intents_count,
                decisions_count=decisions_count,
                submitted_count=submitted_count,
                dry_run=bool(self.dry_run),
                error_message=str(exc),
            )
