from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List


@dataclass(frozen=True)
class ApprovedIntent:
    intent_id:        str
    approval_payload: Any   # PmccOrderSpec | OptionMarketOrderSpec


@dataclass(frozen=True)
class RejectedIntent:
    intent_id: str
    reason:    str


@dataclass(frozen=True)
class RiskDecisionBatch:
    approved: List[ApprovedIntent]
    rejected: List[RejectedIntent]
