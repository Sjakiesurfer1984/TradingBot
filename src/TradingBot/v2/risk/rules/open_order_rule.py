from __future__ import annotations

# dataclass is used to define small, behaviour-focused objects
# with minimal boilerplate. This rule is immutable and stateless.
from dataclasses import dataclass

# Any, Dict, Optional are used to describe broker payloads
# which are not under our control and may have inconsistent shapes.
from typing import Any, Dict, Optional

# RiskContext provides the immutable snapshot for this cycle.
from TradingBot.v2.context import RiskContext

# Symbol is the canonical domain type for asset identifiers.
from TradingBot.v2.domain.types import Symbol

# TradeIntent is the object produced by strategies and consumed by risk rules.
from TradingBot.v2.intents import TradeIntent

from TradingBot.v2.logger import setup_logger
logger = setup_logger("Open Order Rule")

@dataclass(frozen=True)
class OpenOrderDedupeRule:
    """
    Risk rule: reject intents if an open order already exists
    for the same underlying symbol.

    Design intent
    - Prevent duplicate orders for the same underlying within overlapping cycles.
    - Catch both:
      - deterministic duplicates (client_order_id match)
      - heuristic duplicates (symbol appears in order or legs)

    Why this rule exists
    - Brokers may accept multiple orders for the same symbol unless explicitly prevented.
    - Strategies are intentionally dumb about global state.
    - Deduplication belongs in the risk layer.
    """

    # Prefix used when constructing deterministic client_order_id values.
    # This must match the convention used when orders are eventually built.
    client_order_prefix: str = "PMCC"

    @staticmethod
    def _safe_str(value: Any) -> str:
        """
        Convert an unknown broker payload value into a safe string.

        Why this exists
        - Broker payloads are not type-safe.
        - Fields may be missing, None, or non-string.
        - Risk rules must never crash due to malformed broker data.
        """
        return "" if value is None else str(value)

    def _expected_client_order_id(self, underlying: Symbol) -> str:
        """
        Build the deterministic client_order_id used for deduplication.

        Why this exists
        - client_order_id is the strongest dedupe signal when available.
        - It is broker-facing, so it must be a string.
        - Symbol -> str conversion is done here and nowhere else.

        Example
        - underlying = Symbol("SPY")
        - result = "PMCC:SPY"
        """
        return f"{self.client_order_prefix}:{str(underlying).strip().upper()}"

    def _mentions_underlying(self, underlying: Symbol, order: Dict[str, Any]) -> bool:
        """
        Heuristic check: does this broker order appear to reference the underlying?

        Why this exists
        - Not all broker orders will have clean client_order_id values.
        - Multi-leg option orders often embed the underlying symbol
          in leg symbols or order-level symbols.

        Strategy
        - Convert Symbol -> uppercase string once.
        - Inspect:
          - order["symbol"]
          - each leg["symbol"] if legs are present
        """
        # Normalise the underlying symbol for string comparison.
        ul: str = str(underlying).strip().upper()

        # Check top-level order symbol field.
        symbol_field: str = self._safe_str(order.get("symbol")).upper()
        if symbol_field.startswith(ul):
            return True

        # Check individual legs if present.
        legs = order.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                # Defensive validation: legs should be dict-like.
                if not isinstance(leg, dict):
                    continue

                leg_symbol: str = self._safe_str(leg.get("symbol")).upper()
                if leg_symbol.startswith(ul):
                    return True

        # No mention of the underlying was found.
        return False

    def _intent_underlying(self, intent: TradeIntent) -> Symbol:
        """
        Extract the canonical underlying Symbol from a TradeIntent.

        Why this exists
        - Different strategies may encode underlying information differently.
        - This method centralises how we determine "what symbol this intent is about".
        - If new intent payload types are added later, branching happens here.

        Current behaviour
        - For PMCC intents, the underlying is stored in payload.underlying_symbol.
        """
        return intent.payload.underlying_symbol

    def check(self, ctx: RiskContext, intent: TradeIntent) -> Optional[str]:
        """
        Evaluate this rule for a single trade intent.

        Return value
        - None: the rule does not reject the intent.
        - str: the rule rejects the intent, and the string explains why.

        Why this signature
        - Keeps rules simple and composable.
        - RiskEngineV2 can apply many rules and collect rejection reasons.
        """

        # Determine the canonical underlying symbol for this intent.
        underlying: Symbol = self._intent_underlying(intent)

        # Build the deterministic expected client_order_id for this underlying.
        expected_id: str = self._expected_client_order_id(underlying)

        # Iterate through the broker-provided open orders snapshot.
        for order in ctx.open_orders:
            # Defensive check: broker payloads should be dict-like.
            if not isinstance(order, dict):
                continue

            # Primary dedupe path:
            # Exact match on client_order_id.
            client_order_id: str = self._safe_str(
                order.get("client_order_id")
            ).strip()

            if client_order_id == expected_id:
                return (
                    f"Open order exists for {underlying} "
                    f"(client_order_id match)."
                )

            # Secondary dedupe path:
            # Heuristic inspection of symbol fields.
            if self._mentions_underlying(underlying, order):
                return (
                    f"Open order exists for {underlying} "
                    f"(symbol match)."
                )

        # No open order was found that conflicts with this intent.
        return None
