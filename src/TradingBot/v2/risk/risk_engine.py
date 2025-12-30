# src/TradingBot/v2/risk/risk_engine.py
#
# This module defines the central risk engine for V2 (Option C).
#
# Big picture
# - Strategies propose trades by emitting TradeIntent objects (desire).
# - The risk engine evaluates those intents against risk rules (permission).
# - The orchestrator executes only approved results (action).
#
# Why this separation matters
# - It prevents strategies from "accidentally" placing trades.
# - It makes risk logic testable without broker access.
# - It keeps one place where trading permissions are decided.

from __future__ import annotations

# dataclass reduces boilerplate for small classes that mainly store dependencies.
from dataclasses import dataclass

# List is used for ordered collections of items.
# Ordered matters here because we want the output RiskDecision list
# to match the input intents order exactly (debugging and traceability).
from typing import List, Optional

# RiskContext is the immutable snapshot for a single cycle.
# The risk engine reads from it but must never mutate it.
from TradingBot.v2.context import RiskContext

# Decisions are the risk engine outputs.
# We explicitly represent both approval and rejection as first-class outcomes.
from TradingBot.v2.risk.decisions import RejectedIntent, RiskDecision

# TradeIntent is the strategy output type (a request, not an order).
from TradingBot.v2.intents import TradeIntent

# The dedupe rule is one concrete rule in our growing risk ruleset.
from TradingBot.v2.risk.rules.open_order_rule import OpenOrderDedupeRule

from TradingBot.v2.logger import setup_logger
logger = setup_logger("Risk Engine")


@dataclass(frozen=True)
class RiskEngineV2:
    """
    Central risk evaluation engine for V2 Option C.

    Responsibilities
    - Evaluate TradeIntent objects against risk rules.
    - Produce a RiskDecision for every intent.
    - Never talk to the broker (no network IO).
    - Never size or submit orders directly (that comes later via ApprovedOrder).

    Safety stance (current)
    - "Reject by default" is intentional.
    - Until sizing and order construction exist, nothing should be approved.
    """

    # Rule dependency: checks for existing open orders that would conflict.
    # We inject it so the risk engine can be tested with a fake or alternate rule.
    dedupe_rule: OpenOrderDedupeRule

    def _reject(self, intent: TradeIntent, reason: str) -> RiskDecision:
        """
        Helper to build a consistent rejection decision.

        Why a helper exists
        - It prevents copy-paste of the same object construction.
        - It ensures rejection formatting is consistent across rules.
        """
        # RejectedIntent stores the intent_id and a human-readable reason.
        rejected: RejectedIntent = RejectedIntent(
            intent_id=intent.intent_id,
            reason=reason,
        )

        # RiskDecision wraps the outcome so orchestrator can handle it uniformly.
        decision: RiskDecision = RiskDecision(rejected=rejected)
        return decision

    def evaluate(self, ctx: RiskContext, intents: List[TradeIntent]) -> List[RiskDecision]:
        """
        Evaluate a batch of trade intents against current risk rules.

        Parameters
        - ctx:
            Immutable snapshot of account and market state for this cycle.
        - intents:
            Ordered list of TradeIntent objects produced by strategies.

        Returns
        - Ordered list of RiskDecision objects, one per intent.

        Rule evaluation order (current)
        - Open-order dedupe rule
        - Default rejection (because sizing and approval are not implemented)

        Why ordered output matters
        - It lets you correlate input -> output by index during debugging.
        - It keeps logs deterministic.
        """
        # Initialise an empty list that we will fill.
        # Lists are mutable, which is useful when building up results incrementally.
        decisions: List[RiskDecision] = []

        # Iterate over each intent in the order it was produced.
        for intent in intents:
            # Ask the dedupe rule whether this intent should be rejected.
            # The rule returns:
            # - None if it does not reject
            # - A string reason if it rejects
            reason: Optional[str] = self.dedupe_rule.check(ctx, intent)

            # If the rule produced a reason, we reject immediately.
            if reason is not None:
                decisions.append(self._reject(intent=intent, reason=reason))
                continue

            # If we get here, dedupe did not reject the intent.
            #
            # We still reject because "approval/sizing" is not implemented.
            # This is the deliberate safety rail that prevents accidental trading.
            decisions.append(
                self._reject(
                    intent=intent,
                    reason="Not yet approved (sizing not implemented).",
                )
            )

        # Return the decisions list, preserving intent order.
        return decisions
