from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from TradingBot.v2.domain.types import IntentId, StrategyId, Symbol


@dataclass(frozen=True)
class SelectedOption:
    """
    A concrete option contract selected by a strategy.

    Design intent
    - Represents the output of option selection logic only.
    - Contains enough information for the risk layer to reason about cost,
      exposure, and feasibility without re-querying the broker immediately.

    What this is NOT
    - It does not imply approval.
    - It does not imply quantity.
    - It does not imply order submission.

    Why prices are included here
    - Risk sizing requires an estimate of capital usage.
    - Refetching quotes inside the risk layer would introduce latency
      and inconsistency.
    - These values are treated as estimates, not guarantees.
    """

    # Broker-specific option symbol (e.g. SPY260621C00450000).
    option_symbol: str

    # Ask price observed at selection time.
    # Used as a conservative estimate for debit sizing.
    ask_price: float

    # Bid price observed at selection time.
    # Used as a conservative estimate for credit offsets.
    bid_price: float

    # Option delta at selection time.
    # Primarily informational for risk inspection and logging.
    delta: float

    # Days to expiration at selection time.
    # Stored explicitly to avoid recomputation and ambiguity.
    dte: int


@dataclass(frozen=True)
class PmccIntentPayload:
    """
    Payload for a Poor Man's Covered Call (PMCC) intent.

    Design intent
    - Encapsulates all PMCC-specific details in a single object.
    - Keeps TradeIntent generic and strategy-agnostic.

    Why this exists
    - Different strategies will have different payload shapes.
    - This avoids polluting TradeIntent with strategy-specific fields.
    """

    # Underlying equity symbol (e.g. SPY).
    underlying_symbol: Symbol

    # Selected long-dated call (LEAPS leg).
    leap: SelectedOption

    # Selected short-dated call (near-term leg).
    near: SelectedOption


@dataclass(frozen=True)
class TradeIntent:
    """
    A request from a strategy to the risk engine.

    Design intent
    - This object expresses desire, not permission.
    - It is the only thing a strategy is allowed to produce.

    Explicit exclusions
    - No quantity.
    - No capital allocation.
    - No broker order object.
    - No side effects.

    These exclusions are fundamental to Option C.
    """

    # Unique identifier for this intent instance.
    # Deterministically generated using domain.ids.make_intent_id.
    intent_id: IntentId

    # Stable identifier of the originating strategy.
    # Used by the risk layer for per-strategy limits and allocation.
    strategy_id: StrategyId

    # Primary symbol the intent relates to (usually the underlying).
    # Used for deduplication and symbol-level exposure checks.
    symbol: Symbol

    # Strategy-specific payload describing the desired trade structure.
    payload: PmccIntentPayload

    # Requested time-in-force for the eventual order.
    # This is a hint, not a guarantee.
    time_in_force: str = "day"

    # Optional free-form tags for logging, analytics, or future routing.
    # Tuple is used to discourage mutation after creation.
    tags: Tuple[str, ...] = ()
