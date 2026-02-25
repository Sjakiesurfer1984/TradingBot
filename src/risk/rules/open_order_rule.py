from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from src.domain.intents import TradeIntent
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.risk.rules.risk_rule_interface import RiskRuleABC, RiskRuleOutcome
from src.utilities.logger import setup_logger

logger = setup_logger("OpenOrderDedupeRule")


def _underlying(order: Dict[str, Any]) -> str:
    sym = order.get("symbol") or order.get("underlying_symbol") or ""
    return str(sym).strip().upper()


@dataclass
class OpenOrderDedupeRule(RiskRuleABC):
    """Reject an intent if there is already an open order for the same underlying."""

    def evaluate(self, snapshot: CycleSnapshot, intent: TradeIntent) -> RiskRuleOutcome:
        underlying = str(intent.symbol).strip().upper()
        open_orders: List[Dict[str, Any]] = list(snapshot.open_orders())

        for order in open_orders:
            if _underlying(order) == underlying:
                logger.info(
                    "OpenOrderDedupe REJECT | symbol=%s intent_id=%s",
                    underlying, intent.intent_id,
                )
                return RiskRuleOutcome.rejected(
                    f"Open order already exists for {underlying}"
                )

        return RiskRuleOutcome.passed()
