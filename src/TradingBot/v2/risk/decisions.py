from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from TradingBot.domain.orders import MultiLegLimitOrder


@dataclass(frozen=True)
class ApprovedOrder:
    """
    A fully approved and sized order, ready for broker submission.

    Design intent
    - This object represents the final output of the risk engine
      when an intent has passed all risk checks.
    - It is deliberately concrete and execution-ready.
    - Once created, it should be safe for the orchestrator to submit
      without further modification.

    Why this exists
    - Separates "approval" from "submission".
    - Allows dry-run logging and audit trails without touching the broker.
    - Makes the approval decision explicit and inspectable.
    """

    # Identifier of the originating trade intent.
    # This allows traceability from strategy -> intent -> risk decision -> order.
    intent_id: str

    # Deterministic client-side order identifier.
    # Used for deduplication, reconciliation, and post-trade analysis.
    client_order_id: str

    # Fully constructed broker order object.
    # At this point, quantity, legs, and time-in-force are final.
    order: MultiLegLimitOrder


@dataclass(frozen=True)
class RejectedIntent:
    """
    Representation of a rejected trade intent.

    Design intent
    - Every rejected intent must carry a clear, human-readable reason.
    - Rejections are first-class outcomes, not errors or exceptions.

    Why this exists
    - Enables transparent logging and debugging.
    - Allows post-mortem analysis of why trades did not occur.
    - Prevents silent failures or implicit rejections.
    """

    # Identifier of the rejected trade intent.
    intent_id: str

    # Human-readable explanation for the rejection.
    # This should be explicit enough that a human can understand
    # what risk rule blocked the intent.
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    """
    Result of evaluating a single trade intent.

    Invariant
    - Exactly one of `approved` or `rejected` should be populated.
    - Both being None or both being set indicates a programming error.

    Why this wrapper exists
    - Makes the outcome of risk evaluation explicit.
    - Allows the orchestrator to handle approvals and rejections uniformly.
    - Keeps the risk engine interface stable as more decision types are added.
    """

    # Populated when the intent is approved and converted into an executable order.
    approved: Optional[ApprovedOrder] = None

    # Populated when the intent is rejected by any risk rule.
    rejected: Optional[RejectedIntent] = None
