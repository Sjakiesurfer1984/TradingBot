from __future__ import annotations

# dataclass is used to define small, behaviour-focused objects
# with minimal boilerplate. This rule is immutable and stateless.
from dataclasses import dataclass

# Any, Dict, Optional are used to describe broker payloads
# which are not under our control and may have inconsistent shapes.
from typing import Any, Dict, Optional


# Symbol is the canonical domain type for asset identifiers.
from TradingBot.domain.types import Symbol, ClientOrderId

from TradingBot.orchestration.context import RiskContext
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope
from TradingBot.domain.intents import (
    TradeIntent,
    PmccIntentPayload,
)
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

    def _expected_client_order_id(self, underlying: Symbol) -> ClientOrderId:
        expected_raw: str = f"{self.client_order_prefix}:{str(underlying).strip().upper()}"
        return ClientOrderId(expected_raw)


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
        with log_scope("open_order_rule._mentions_underlying", logger, extra=f"underlying={underlying}"):
            # Normalise the underlying symbol for string comparison.
            ul: str = str(underlying).strip().upper()

            # Check top-level order symbol field.
            symbol_field: str = self._safe_str(order.get("symbol")).upper()
            if symbol_field.startswith(ul):
                logger.info("Underlying mentioned in order.symbol | underlying=%s order_symbol=%s", ul, symbol_field)
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
                        logger.info(
                            "Underlying mentioned in order.legs | underlying=%s leg_symbol=%s",
                            ul,
                            leg_symbol,
                        )
                        return True

            # No mention of the underlying was found.
            logger.info("Underlying not mentioned in order payload | underlying=%s", ul)
            return False

    def _intent_underlying(self, intent: TradeIntent) -> Symbol:
        payload = intent.payload

        if isinstance(payload, PmccIntentPayload):
            return payload.underlying_symbol

        raise TypeError(
            f"Unsupported intent payload type for underlying extraction: {type(payload).__name__}"
        )


    def check(self, ctx: RiskContext, intent: TradeIntent) -> Optional[str]:
        """
        Evaluate this rule for a single trade intent.

        Return value
        - None: the rule does not reject the intent.
        - str: the rule rejects the intent, and the string explains why.
        """
        with log_scope(
            "open_order_rule.check",
            logger,
            extra=f"intent_id={intent.intent_id} strategy_id={intent.strategy_id} symbol={intent.symbol}",
        ):
            underlying: Symbol = self._intent_underlying(intent)
            logger.info(
                "Checking open-order dedupe | underlying=%s open_orders=%d",
                str(underlying),
                int(len(ctx.open_orders)),
            )

            expected_id: ClientOrderId = self._expected_client_order_id(underlying)
            expected_id_str: str = str(expected_id)
            logger.info("Expected client_order_id computed | expected_id=%s", expected_id_str)

            for i, order_any in enumerate(ctx.open_orders):
                if not isinstance(order_any, dict):
                    logger.warning(
                        "Skipping non-dict open order payload | index=%d type=%s",
                        int(i),
                        type(order_any).__name__,
                    )
                    continue

                order: Dict[str, Any] = order_any
                
                client_order_id_str: str = self._safe_str(order.get("client_order_id")).strip()
                if client_order_id_str:
                    logger.info(
                        "Open order client_order_id observed | index=%d client_order_id=%s",
                        int(i),
                        client_order_id_str,
                    )

                expected_prefix: str = f"{self.client_order_prefix}:{str(underlying).strip().upper()}:"
                
                if client_order_id_str.startswith(expected_prefix):
                    reason: str = f"Open order exists for {underlying} (client_order_id prefix match)."
                    logger.info(
                        "Dedupe hit | index=%d reason=%s client_order_id=%s expected_prefix=%s",
                        int(i),
                        reason,
                        client_order_id_str,
                        expected_prefix,
                    )
                    return reason

                if self._mentions_underlying(underlying, order):
                    reason = f"Open order exists for {underlying} (symbol match)."
                    logger.info("Dedupe hit | index=%d reason=%s", int(i), reason)
                    return reason

            logger.info("No conflicting open orders found | underlying=%s", str(underlying))
            return None

