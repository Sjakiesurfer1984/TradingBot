from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from TradingBot.domain.intents import TradeIntent
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC


@dataclass(frozen=True)
class RiskRuleOutcome:
    """
    Outcome of evaluating a single rule against a single intent.
    """

    allowed: bool
    reason: Optional[str] = None

    @staticmethod
    def allow() -> "RiskRuleOutcome":
        return RiskRuleOutcome(allowed=True, reason=None)

    @staticmethod
    def reject(reason: str) -> "RiskRuleOutcome":
        return RiskRuleOutcome(allowed=False, reason=reason)


class RiskRuleABC(ABC):
    """
    Risk rule contract (UML).

    Each rule evaluates exactly one intent at a time.
    """

    @abstractmethod
    def apply(self, snapshot: CycleSnapshotABC, intent: TradeIntent) -> RiskRuleOutcome:
        raise NotImplementedError
