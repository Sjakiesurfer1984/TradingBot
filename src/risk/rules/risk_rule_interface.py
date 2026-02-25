from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.domain.intents import TradeIntent
from src.orchestration.cycle_snapshot import CycleSnapshot

class RiskRuleOutcomeKind(str, Enum):
    PASS   = "pass"
    REJECT = "reject"


@dataclass(frozen=True)
class RiskRuleOutcome:
    kind:   RiskRuleOutcomeKind
    reason: Optional[str] = None

    @classmethod
    def passed(cls) -> "RiskRuleOutcome":
        return cls(kind=RiskRuleOutcomeKind.PASS)

    @classmethod
    def rejected(cls, reason: str) -> "RiskRuleOutcome":
        return cls(kind=RiskRuleOutcomeKind.REJECT, reason=reason)

    @property
    def is_pass(self) -> bool:
        return self.kind == RiskRuleOutcomeKind.PASS


class RiskRuleABC(ABC):
    @abstractmethod
    def evaluate(self, snapshot: CycleSnapshot, intent: TradeIntent) -> RiskRuleOutcome:
        raise NotImplementedError
