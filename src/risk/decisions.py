from __future__ import annotations

from dataclasses import dataclass
from typing import List

from src.domain.intents import IntentPayloadABC


@dataclass(frozen=True)
class ApprovedIntent:
    intent_id: str
    payload:   IntentPayloadABC   # typed — no Any, no type-erasure hack


@dataclass(frozen=True)
class RejectedIntent:
    intent_id: str
    reason:    str


@dataclass(frozen=True)
class RiskDecisionBatch:
    approved: List[ApprovedIntent]
    rejected: List[RejectedIntent]