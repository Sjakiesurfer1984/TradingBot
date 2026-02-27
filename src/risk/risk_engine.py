from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from src.domain.intents import TradeIntent
from src.domain.types import StrategyId
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.risk.decisions import ApprovedIntent, RejectedIntent, RiskDecisionBatch
from src.risk.evaluators import IntentEvaluatorABC
from src.risk.rules.risk_rule_interface import RiskRuleABC
from src.utilities.logger import setup_logger

logger = setup_logger("RiskEngine")


@dataclass
class RiskEngine:
    """
    Two-phase risk evaluation pipeline.

    Phase 1 — Chain-of-Responsibility rules (gates):
        Each RiskRuleABC is checked in registration order.
        First rejection stops the chain and rejects the intent.
        Rules are strategy-agnostic (e.g. open-order deduplication).

    Phase 2 — Strategy evaluator:
        Dispatches to the evaluator registered for the intent's strategy_id.
        One evaluator per strategy — it handles all payload types for that
        strategy internally. This avoids a per-payload-type registry that
        would require updating every time a strategy adds a new action.

    OCP:  adding a new strategy = register one evaluator. Zero other changes.
    DIP:  depends on IntentEvaluatorABC, never on concrete evaluators.
    """

    rules:       List[RiskRuleABC]              = field(default_factory=list)
    _evaluators: Dict[str, IntentEvaluatorABC]  = field(default_factory=dict, init=False)

    def register(
        self,
        strategy_id: str,
        evaluator:   IntentEvaluatorABC,
    ) -> None:
        self._evaluators[strategy_id] = evaluator
        logger.info(
            "Registered evaluator | strategy=%s evaluator=%s",
            strategy_id, type(evaluator).__name__,
        )

    def evaluate(
        self,
        snapshot: CycleSnapshot,
        intents:  List[TradeIntent],
    ) -> RiskDecisionBatch:
        approved: List[ApprovedIntent] = []
        rejected: List[RejectedIntent] = []

        for intent in intents:

            # Phase 1 — chain-of-responsibility gates
            rule_rejection = None
            for rule in self.rules:
                outcome = rule.evaluate(snapshot, intent)
                if not outcome.is_pass:
                    rule_rejection = outcome.reason
                    break

            if rule_rejection:
                logger.info(
                    "Intent REJECTED by rule | id=%s reason=%s",
                    intent.intent_id, rule_rejection,
                )
                rejected.append(RejectedIntent(
                    intent_id=str(intent.intent_id),
                    reason=rule_rejection,
                ))
                continue

            # Phase 2 — strategy evaluator dispatch by strategy_id
            evaluator = self._evaluators.get(str(intent.strategy_id))
            if evaluator is None:
                reason = (
                    f"No evaluator registered for strategy_id='{intent.strategy_id}'"
                )
                logger.warning("Intent REJECTED | id=%s reason=%s", intent.intent_id, reason)
                rejected.append(RejectedIntent(intent_id=str(intent.intent_id), reason=reason))
                continue

            result = evaluator.evaluate(snapshot, intent)
            if result.approved is not None:
                approved.append(result.approved)
            elif result.rejected is not None:
                logger.info(
                    "Intent REJECTED by evaluator | id=%s reason=%s",
                    result.rejected.intent_id, result.rejected.reason,
                )
                rejected.append(result.rejected)

        logger.info(
            "Risk evaluation complete | approved=%d rejected=%d",
            len(approved), len(rejected),
        )
        return RiskDecisionBatch(approved=approved, rejected=rejected)