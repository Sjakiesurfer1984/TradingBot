# src/TradingBot/v2/risk/risk_engine.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Context and decision imports
from TradingBot.v2.context import RiskContext
from TradingBot.v2.risk.decisions import ApprovedOrder, RejectedIntent, RiskDecision
from TradingBot.v2.intents import TradeIntent, PmccIntentPayload
from TradingBot.v2.risk.rules.open_order_rule import OpenOrderDedupeRule

# PMCC sizing and symbol parsing
from TradingBot.v2.pmcc_sizer import PmccSizer, parse_opra

# Domain types and order builders
from TradingBot.v2.domain.orders import (
    OptionLeg as DomainOptionLeg,
    OrderSide as DomainOrderSide,
    MultiLegLimitOrder,
)
from TradingBot.v2.domain.types import ClientOrderId, normalise_symbol
from decimal import Decimal

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

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
    """

    dedupe_rule: OpenOrderDedupeRule
    pmcc_sizer: PmccSizer  # Injected PMCC sizing component

    def _reject(self, intent: TradeIntent, reason: str) -> RiskDecision:
        with log_scope("risk_engine._reject", logger, extra=f"intent_id={intent.intent_id}"):
            rejected: RejectedIntent = RejectedIntent(
                intent_id=intent.intent_id,
                reason=reason,
            )
            decision: RiskDecision = RiskDecision(rejected=rejected)
            logger.info(
                "Rejected intent | intent_id=%s strategy_id=%s symbol=%s reason=%s",
                str(intent.intent_id),
                str(intent.strategy_id),
                str(intent.symbol),
                reason,
            )
            return decision

    def evaluate(self, ctx: RiskContext, intents: List[TradeIntent]) -> List[RiskDecision]:
        with log_scope("risk_engine.evaluate", logger, extra=f"intents={len(intents)} as_of_utc={ctx.as_of_utc.isoformat()}"):
            decisions: List[RiskDecision] = []

            for idx, intent in enumerate(intents):
                with log_scope(
                    "risk_engine.evaluate_intent",
                    logger,
                    extra=f"index={idx} intent_id={intent.intent_id} strategy_id={intent.strategy_id} symbol={intent.symbol}",
                ):
                    # Dedupe rule first
                    reason: Optional[str] = self.dedupe_rule.check(ctx, intent)
                    if reason is not None:
                        logger.info("Dedupe rule rejected intent | intent_id=%s reason=%s", str(intent.intent_id), reason)
                        decisions.append(self._reject(intent=intent, reason=reason))
                        continue

                    # Route PMCC intents to sizing + approval
                    payload = intent.payload
                    if isinstance(payload, PmccIntentPayload):
                        try:
                            sizing = self.pmcc_sizer.size(
                                equity=ctx.equity,
                                option_buying_power=ctx.option_buying_power,
                                leap=payload.leap_leg.contract,
                                near=payload.near_leg.contract,
                            )
                        except ValueError as ex:
                            logger.info(
                                "PMCC sizing rejected intent | intent_id=%s reason=%s",
                                str(intent.intent_id),
                                str(ex),
                            )
                            decisions.append(self._reject(intent=intent, reason=str(ex)))
                            continue

                        # Parse option symbols and build domain legs
                        leap_contract = parse_opra(payload.leap_leg.contract.option_symbol)
                        near_contract = parse_opra(payload.near_leg.contract.option_symbol)

                        long_leg = DomainOptionLeg(
                            contract=leap_contract,
                            side=DomainOrderSide.BUY,
                            ratio=int(payload.leap_leg.ratio),
                        )
                        short_leg = DomainOptionLeg(
                            contract=near_contract,
                            side=DomainOrderSide.SELL,
                            ratio=int(payload.near_leg.ratio),
                        )

                        underlying_sym = normalise_symbol(payload.underlying_symbol)
                        client_order_id: ClientOrderId = ClientOrderId(f"PMCC:{underlying_sym}")

                        order = MultiLegLimitOrder(
                            client_order_id=client_order_id,
                            underlying=underlying_sym,
                            legs=(long_leg, short_leg),
                            quantity=sizing.qty,
                            limit_price=Decimal(str(sizing.limit_price)),
                        )

                        approved = ApprovedOrder(
                            intent_id=intent.intent_id,
                            client_order_id=client_order_id,
                            order=order,
                        )
                        decisions.append(RiskDecision(approved=approved))
                        continue

                    # All other intent types are rejected by default
                    logger.info("Dedupe passed. Applying default safety rejection | intent_id=%s", str(intent.intent_id))
                    decisions.append(
                        self._reject(
                            intent=intent,
                            reason="Not yet approved (sizing not implemented).",
                        )
                    )

            logger.info("Risk evaluation complete | decisions=%d", int(len(decisions)))
            return decisions
