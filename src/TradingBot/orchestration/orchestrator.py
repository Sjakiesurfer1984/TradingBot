from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Set

from TradingBot.brokers.broker_interface import BrokerABC

from TradingBot.domain.intents import TradeIntent
from TradingBot.domain.orders import OrderABC
from TradingBot.domain.types import Symbol

from TradingBot.execution.execution_policy_interface import ExecutionPolicyABC

from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.orchestration.cycle_snapshot_builder import CycleSnapshotBuilderABC, CycleSnapshotBuilder
from TradingBot.orchestration.orchestrator_interface import CycleRunResult, CycleRunStatus, OrchestratorABC

from TradingBot.risk.decisions import ApprovedIntent, RiskDecision
from TradingBot.risk.risk_engine import RiskEngineABC

from TradingBot.strategies.option_chain_consumer import OptionChainConsumerABC
from TradingBot.strategies.strategy_interface import StrategyABC

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

from TradingBot.signals.signal_pipeline_interface import SignalPipelineABC
from TradingBot.signals.default_signal_pipeline import DefaultSignalPipeline


logger = setup_logger("TradingOrchestrator")


@dataclass
class TradingOrchestrator(OrchestratorABC):
    broker: BrokerABC
    strategies: List[StrategyABC]
    risk_engine: RiskEngineABC
    execution_policy: ExecutionPolicyABC
    snapshot_builder: CycleSnapshotBuilderABC
    signal_pipeline: SignalPipelineABC = field(default_factory=DefaultSignalPipeline)
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
            for req in orders:
                order_obj = self.broker.submit_order(req)
                submitted += 1

                order_id = getattr(order_obj, "id", None)
                status = getattr(order_obj, "status", None)
                order_type = getattr(order_obj, "order_type", getattr(order_obj, "type", None))
                limit_price = getattr(order_obj, "limit_price", None)

                # Parent order log
                logger.info(
                    "ORDER SUBMITTED | parent_id=%s status=%s type=%s limit=%s",
                    order_id,
                    status,
                    order_type,
                    limit_price,
                )

                legs = getattr(order_obj, "legs", None)

                if legs:
                    for leg in legs:
                        logger.info(
                            "ORDER LEG | parent_id=%s leg_id=%s symbol=%s side=%s qty=%s status=%s",
                            order_id,
                            getattr(leg, "id", None),
                            getattr(leg, "symbol", None),
                            getattr(leg, "side", None),
                            getattr(leg, "qty", None),
                            getattr(leg, "status", None),
                        )
                else:
                    # Single-leg fallback (in case it's not multi-leg)
                    logger.info(
                        "ORDER SINGLE | parent_id=%s symbol=%s side=%s qty=%s status=%s",
                        order_id,
                        getattr(order_obj, "symbol", None),
                        getattr(order_obj, "side", None),
                        getattr(order_obj, "qty", None),
                        status,
                    )
        return submitted

    def run_cycle(self) -> CycleRunResult:
        started_at_utc: datetime = self._now_utc()
        intents_count: int = 0
        decisions_count: int = 0
        submitted_count: int = 0

        try:
            with log_scope("orchestrator.run_cycle", logger):
                snapshot: CycleSnapshotABC = self._build_cycle_snapshot()
                signals = self.signal_pipeline.build(snapshot)

                if hasattr(snapshot, "with_signals"):
                    snapshot = snapshot.with_signals(signals)  # type: ignore[assignment]
                else:
                    # Defensive: should not happen once CycleSnapshot is updated.
                    pass

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
