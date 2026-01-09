# src/TradingBot/v2/risk/risk_engine.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Any

# Context and decision imports
from TradingBot.v2.context import RiskContext
from TradingBot.v2.risk.decisions import ApprovedOrder, RejectedIntent, RiskDecision
from TradingBot.v2.intents import TradeIntent, PmccIntentPayload
from TradingBot.v2.risk.rules.open_order_rule import OpenOrderDedupeRule

# PMCC sizing and symbol parsing
from TradingBot.v2.pmcc_sizer import PmccSizer

# Domain types and order builders

from TradingBot.v2.domain.orders import (
    MultiLegLimitOrder,
    OptionContract,
    OptionLeg as DomainOptionLeg,
    OptionRight,
    OrderSide as DomainOrderSide,
    TimeInForce,
)
from TradingBot.v2.domain.types import ClientOrderId, normalise_symbol
from decimal import Decimal

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

from TradingBot.v2.pmcc_sizer import parse_osi

from decimal import Decimal


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

    dedupe_rule: OpenOrderDedupeRule # Injected deduplication rule
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
    def _approve(self, *, intent: TradeIntent, order: Any) -> RiskDecision:
        with log_scope("risk_engine._approved", logger, extra=f"intent_id={intent.intent_id}"):

            return RiskDecision(
                intent_id=intent.intent_id,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                approved=True,
                reason=None,
                client_order_id=str(getattr(order, "client_order_id", "")),
                order=order,
            )

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

                        # Parse option symbols (variable-length OSI root supported)
                        leap_parsed = parse_osi(payload.leap_leg.contract.option_symbol)
                        near_parsed = parse_osi(payload.near_leg.contract.option_symbol)

                        # Build domain contracts from parsed OSI
                        leap_contract = OptionContract(
                            underlying=payload.underlying_symbol,
                            expiry=leap_parsed.expiry_utc.replace(tzinfo=None),
                            strike=leap_parsed.strike.quantize(Decimal("0.01")),
                            right=OptionRight.CALL if leap_parsed.right == "call" else OptionRight.PUT,
                            option_symbol=leap_parsed.option_symbol,
                        )

                        near_contract = OptionContract(
                            underlying=payload.underlying_symbol,
                            expiry=near_parsed.expiry_utc.replace(tzinfo=None),
                            strike=near_parsed.strike.quantize(Decimal("0.01")),
                            right=OptionRight.CALL if near_parsed.right == "call" else OptionRight.PUT,
                            option_symbol=near_parsed.option_symbol,
                        )

                        long_leg = DomainOptionLeg(
                            contract=leap_contract,
                            side=DomainOrderSide.BUY,
                            ratio=1,
                        )

                        short_leg = DomainOptionLeg(
                            contract=near_contract,
                            side=DomainOrderSide.SELL,
                            ratio=1,
                        )

                        # Deterministic client order id, do not pull it off RiskDecision
                        underlying_sym = normalise_symbol(str(payload.underlying_symbol))
                        client_order_id = f"PMCC:{str(underlying_sym)}"

                        order = MultiLegLimitOrder(
                            client_order_id=client_order_id,
                            underlying=underlying_sym,
                            legs=(long_leg, short_leg),
                            quantity=int(sizing.qty),
                            limit_price=Decimal(str(sizing.limit_price)).quantize(Decimal("0.0001")),
                            time_in_force=TimeInForce.DAY,
                        )

                        decisions.append(self._approve(intent=intent, order=order))
                        continue


            logger.info("Risk evaluation complete | decisions=%d", int(len(decisions)))
            return decisions
