from __future__ import annotations

from dataclasses import dataclass
from typing import List

from TradingBot.v2.context import RiskContext
from TradingBot.v2.decisions import RejectedIntent, RiskDecision
from TradingBot.v2.intents import TradeIntent
from TradingBot.v2.rules.open_order_rule import OpenOrderDedupeRule


@dataclass
class RiskEngineV2:
    """
    Central risk evaluation engine for Option C (V2 architecture).

    Design intent
    - This class is the single authority that decides whether an intent
      may become an executable order.
    - Strategies never bypass this class.
    - Orchestrator never makes risk decisions itself.

    Current scope (Commit 2)
    - Only open-order deduplication is implemented.
    - All intents are explicitly rejected unless blocked earlier.

    Important
    - This "reject by default" behaviour is deliberate.
    - It ensures that no trading can occur until sizing and approval
      logic are implemented in later commits.
    """

    # Rule responsible for detecting duplicate or conflicting open orders.
    # Additional rules will be added later (allocation, sizing, exposure caps, etc.).
    dedupe_rule: OpenOrderDedupeRule

    def evaluate(self, ctx: RiskContext, intents: List[TradeIntent]) -> List[RiskDecision]:
        """
        Evaluate a batch of trade intents against current risk rules.

        Parameters
        - ctx:
            Immutable snapshot of account and market state for this cycle.
        - intents:
            List of trade intents produced by strategies during this cycle.

        Returns
        - A list of RiskDecision objects.
        - Exactly one RiskDecision is returned per intent, preserving order.

        Behaviour in this commit
        - If an intent violates the open-order deduplication rule, it is rejected
          with a specific, human-readable reason.
        - If an intent passes deduplication, it is still rejected with a
          placeholder message, because sizing and approval are not yet implemented.

        This conservative behaviour acts as a hard safety rail.
        """
        decisions: List[RiskDecision] = []

        for intent in intents:
            # First gate: check whether an open order already exists
            # for the same underlying or client_order_id pattern.
            reason = self.dedupe_rule.check(ctx, intent)

            if reason:
                # Explicit rejection due to deduplication rule.
                decisions.append(
                    RiskDecision(
                        rejected=RejectedIntent(
                            intent_id=intent.intent_id,
                            reason=reason,
                        )
                    )
                )
            else:
                # Default rejection path.
                #
                # This is intentional at this stage of development.
                # Until sizing, allocation, and order construction are implemented,
                # no intent should ever be approved.
                decisions.append(
                    RiskDecision(
                        rejected=RejectedIntent(
                            intent_id=intent.intent_id,
                            reason="Not yet approved (sizing not implemented).",
                        )
                    )
                )

        return decisions
