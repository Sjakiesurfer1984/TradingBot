from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from TradingBot.v2.domain.orders import MultiLegLimitOrder
from TradingBot.v2.domain.types import ClientOrderId, IntentId

from TradingBot.v2.logger import setup_logger
logger = setup_logger("Decisions")

@dataclass(frozen=True)
class ApprovedOrder:
    """
    A fully approved and sized order, ready for broker submission.

    Design intent
    - This is the final output of the risk engine when an intent has passed all checks.
    - This object must be execution-ready, meaning:
        - quantity is final
        - legs are final
        - limit price and time in force are final
    - The orchestrator can submit it without modifying it.

    Why this exists
    - Keeps "approval" separate from "submission".
    - Supports dry-run mode by logging approved orders without touching the broker.
    - Preserves an audit trail for strategy -> intent -> decision -> order.
    """

    # Identifier of the originating trade intent.
    # We use IntentId (a NewType over str) to avoid mixing IDs accidentally.
    intent_id: IntentId

    # Deterministic client-side order identifier.
    # This is how we dedupe and later reconcile what we submitted.
    client_order_id: ClientOrderId

    # The final broker-agnostic order description.
    # Broker adapters translate this into actual API calls.
    order: MultiLegLimitOrder


@dataclass(frozen=True)
class RejectedIntent:
    """
    Representation of a rejected trade intent.

    Design intent
    - Rejections are normal outcomes, not exceptions.
    - Every rejection must contain a human-readable reason.

    Why this exists
    - Makes debugging transparent.
    - Enables post-mortem analysis: "why did we not trade?"
    - Avoids silent failures.
    """

    # Identifier of the rejected trade intent.
    intent_id: IntentId

    # Human-readable explanation for why the intent was rejected.
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    """
    Result of evaluating a single trade intent.

    Invariant (required behaviour)
    - Exactly one of `approved` or `rejected` must be populated.
    - If both are None, the risk engine failed to decide.
    - If both are set, the risk engine produced an invalid outcome.

    Why this wrapper exists
    - Gives the orchestrator a single uniform output type to handle.
    - Keeps the risk engine interface stable as we add more decision categories later.
    """

    # Populated when the intent is approved and converted into an executable order.
    approved: Optional[ApprovedOrder] = None

    # Populated when the intent is rejected.
    rejected: Optional[RejectedIntent] = None

    def is_valid(self) -> bool:
        """
        Return True if this decision satisfies the invariant.

        Why this exists
        - Lets the orchestrator and tests assert correctness explicitly.
        - Keeps invariant checking local to the type.
        """
        approved_set: bool = self.approved is not None
        rejected_set: bool = self.rejected is not None
        return approved_set != rejected_set
