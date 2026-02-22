from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Type

from src.domain.intents import IntentPayloadABC, TradeIntent
from src.orchestration.cycle_snapshot import CycleSnapshotABC
from src.risk.decisions import ApprovedIntent, RejectedIntent, RiskDecisionBatch
from src.risk.evaluators import IntentEvaluatorABC
from src.risk.rules.risk_rule_interface import RiskRuleABC
from src.utilities.logger import setup_logger

logger = setup_logger("RiskEngine")


@dataclass
class RiskEngine:
    rules:      List[RiskRuleABC]                              = field(default_factory=list)
    _evaluators: Dict[Type[IntentPayloadABC], IntentEvaluatorABC] = field(default_factory=dict, init=False)

    def register(self, payload_type: Type[IntentPayloadABC], evaluator: IntentEvaluatorABC) -> None:
        self._evaluators[payload_type] = evaluator
        logger.info("Registered evaluator | payload=%s evaluator=%s",
                    payload_type.__name__, type(evaluator).__name__)

    def evaluate(
        self,
        snapshot: CycleSnapshotABC,
        intents:  List[TradeIntent],
    ) -> RiskDecisionBatch:
        approved: List[ApprovedIntent] = []
        rejected: List[RejectedIntent] = []

        for intent in intents:
            # 1 — run chain-of-responsibility rules first
            rule_rejection = None
            for rule in self.rules:
                outcome = rule.evaluate(snapshot, intent)
                if not outcome.is_pass:
                    rule_rejection = outcome.reason
                    break

            if rule_rejection:
                logger.info("Intent REJECTED by rule | id=%s reason=%s", intent.intent_id, rule_rejection)
                rejected.append(RejectedIntent(intent_id=str(intent.intent_id), reason=rule_rejection))
                continue

            # 2 — dispatch to payload-specific evaluator
            evaluator = self._evaluators.get(type(intent.payload))
            if evaluator is None:
                reason = f"No evaluator for payload type {type(intent.payload).__name__}"
                logger.warning("Intent REJECTED — %s", reason)
                rejected.append(RejectedIntent(intent_id=str(intent.intent_id), reason=reason))
                continue

            result = evaluator.evaluate(snapshot, intent)
            if result.approved is not None:
                approved.append(result.approved)
            elif result.rejected is not None:
                rejected.append(result.rejected)

        logger.info("Risk evaluation complete | approved=%d rejected=%d", len(approved), len(rejected))
        return RiskDecisionBatch(approved=approved, rejected=rejected)
