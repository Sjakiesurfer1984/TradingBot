from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

from TradingBot.domain.types import ClientOrderId, IntentId, StrategyId, Symbol
from TradingBot.utilities.logger import setup_logger

logger = setup_logger("Decisions")

Tapproval_payload = TypeVar("Tapproval_payload")


@dataclass(frozen=True)
class ApprovedIntent(Generic[Tapproval_payload]):
    """
    A risk-approved intent in a policy-neutral form.

    Contract
    - Produced by RiskEngine.
    - Contains sizing and execution parameters, but no executable OrderABC object.
    - ExecutionPolicy converts this approval_payload into broker-agnostic domain orders.
    """

    intent_id: IntentId
    strategy_id: StrategyId
    symbol: Symbol

    # Deterministic client-side identifier used for dedupe and reconciliation.
    client_order_id: ClientOrderId

    # Policy-neutral approval_payload (e.g. PMCC spread approval_payload).
    approval_payload: Tapproval_payload


@dataclass(frozen=True)
class RejectedIntent:
    """
    Representation of a rejected trade intent.
    """

    intent_id: str
    strategy_id: str
    symbol: str
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    """
    Result of evaluating a single trade intent.

    Invariant
    - Exactly one of `approved` or `rejected` must be populated.
    """

    approved: Optional[ApprovedIntent] = None
    rejected: Optional[RejectedIntent] = None

    def is_valid(self) -> bool:
        approved_set: bool = self.approved is not None
        rejected_set: bool = self.rejected is not None
        return approved_set != rejected_set
