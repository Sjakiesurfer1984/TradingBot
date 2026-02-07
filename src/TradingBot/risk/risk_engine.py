from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, TypeVar

from TradingBot.orchestration.context import RiskContext
from TradingBot.risk.pmcc_sizer import parse_osi, PmccSizer

from TradingBot.domain.orders import (
    MultiLegLimitOrder,
    OptionContract,
    OptionLeg as DomainOptionLeg,
    OrderSide as DomainOrderSide,
    OptionRight,
    TimeInForce,
)

from TradingBot.domain.types import ClientOrderId, Symbol, normalise_symbol
from TradingBot.domain.intents import PmccIntentPayload, TradeIntent

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

from TradingBot.risk.decisions import ApprovedOrder, RejectedIntent, RiskDecision
from TradingBot.risk.rules.open_order_rule import OpenOrderDedupeRule

logger = setup_logger("RiskEngine")

TOrder = TypeVar("TOrder")


@dataclass(frozen=True)
class RiskEngine:
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

    dedupe_rule: OpenOrderDedupeRule  # Injected deduplication rule
    pmcc_sizer: PmccSizer  # Injected PMCC sizing component

    # Step 3: hard cap for PMCC units per underlying (temporary fixed policy)
    pmcc_max_units_per_underlying: int = 2

    def _reject(self, intent: TradeIntent, reason: str) -> RiskDecision:
        with log_scope("risk_engine._reject", logger, extra=f"intent_id={intent.intent_id}"):
            rejected: RejectedIntent = RejectedIntent(
                intent_id=intent.intent_id,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
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

    def _approve(self, *, intent: TradeIntent, order: object) -> RiskDecision:
        with log_scope(
            "risk_engine._approved",
            logger,
            extra=f"intent_id={intent.intent_id}",
        ):
            client_order_id: Optional[ClientOrderId] = getattr(order, "client_order_id", None)
            if client_order_id is None:
                raise ValueError("Approved order is missing client_order_id")

            return RiskDecision(
                approved=ApprovedOrder(
                    intent_id=intent.intent_id,
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    client_order_id=client_order_id,
                    order=order,
                )
            )

    @staticmethod
    def _safe_str(v: Any) -> str:
        return "" if v is None else str(v)

    def _count_pmcc_units_from_positions(self, *, ctx: RiskContext, underlying: Symbol) -> int:
        """
        Count approximate PMCC "units" for an underlying using broker positions.

        Definition (minimal, for now)
        - A PMCC unit is 1 long call (LEAP-ish) + 1 short call (near-ish) for the same underlying.
        - We count:
            long_calls = number of call option positions with qty > 0
            short_calls = number of call option positions with qty < 0
          and units = min(long_calls, short_calls)

        Notes
        - This is intentionally rough because broker position payloads differ.
        - Alpaca options positions typically include:
            symbol: OCC/OSI option symbol (string starting with underlying root)
            qty: string number, positive/negative for long/short
            asset_class: "us_option" (sometimes)
        - If the payload shape changes, we fail safe by under-counting rather than crashing.
        """
        ul: str = str(underlying).strip().upper()
        long_calls: int = 0
        short_calls: int = 0

        for p_any in ctx.positions:
            if not isinstance(p_any, dict):
                continue
            p: Dict[str, Any] = p_any

            sym: str = self._safe_str(p.get("symbol")).strip().upper()
            if not sym:
                continue

            # Quick filter: options symbols usually begin with the underlying root.
            # For QQQT calls, symbol often begins with "QQQT" (variable-length roots supported).
            if not sym.startswith(ul):
                continue

            # Determine if this looks like a call option via OSI parsing.
            try:
                parsed = parse_osi(sym)
            except Exception:
                continue

            if parsed.right != "call":
                continue

            qty_raw: Any = p.get("qty")
            try:
                qty: float = float(qty_raw)
            except Exception:
                continue

            if qty > 0:
                long_calls += 1
            elif qty < 0:
                short_calls += 1

        units: int = min(long_calls, short_calls)
        logger.info(
            "PMCC units from positions | underlying=%s long_calls=%d short_calls=%d units=%d max=%d",
            ul,
            int(long_calls),
            int(short_calls),
            int(units),
            int(self.pmcc_max_units_per_underlying),
        )
        return int(units)

    def evaluate(self, ctx: RiskContext, intents: List[TradeIntent]) -> List[RiskDecision]:
        with log_scope(
            "risk_engine.evaluate",
            logger,
            extra=f"intents={len(intents)} as_of_utc={ctx.as_of_utc.isoformat()}",
        ):
            decisions: List[RiskDecision] = []

            for idx, intent in enumerate(intents):
                with log_scope(
                    "risk_engine.evaluate_intent",
                    logger,
                    extra=f"index={idx} intent_id={intent.intent_id} strategy_id={intent.strategy_id} symbol={intent.symbol}",
                ):
                    payload = intent.payload

                    # ----------------------------------------------------------
                    # Step 3: Position-based PMCC unit cap (do this BEFORE dedupe)
                    # ----------------------------------------------------------
                    if isinstance(payload, PmccIntentPayload):
                        underlying_sym: Symbol = normalise_symbol(str(payload.underlying_symbol))
                        units: int = self._count_pmcc_units_from_positions(ctx=ctx, underlying=underlying_sym)

                        if units >= int(self.pmcc_max_units_per_underlying):
                            limit_reason: str = (
                                f"PMCC position cap reached for {underlying_sym}. "
                                f"units={units} max={self.pmcc_max_units_per_underlying}"
                            )
                            logger.info(
                                "PMCC position limit rejected intent | intent_id=%s reason=%s",
                                str(intent.intent_id),
                                limit_reason,
                            )
                            decisions.append(self._reject(intent=intent, reason=limit_reason))
                            continue

                    # Dedupe rule next
                    dedupe_reason: Optional[str] = self.dedupe_rule.check(ctx, intent)
                    if dedupe_reason is not None:
                        logger.info(
                            "Dedupe rule rejected intent | intent_id=%s reason=%s",
                            str(intent.intent_id),
                            dedupe_reason,
                        )
                        decisions.append(self._reject(intent=intent, reason=dedupe_reason))
                        continue

                    # -----------------------------
                    # PMCC: sizing + order building
                    # -----------------------------
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

                        leap_parsed = parse_osi(payload.leap_leg.contract.option_symbol)
                        near_parsed = parse_osi(payload.near_leg.contract.option_symbol)

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

                        underlying_sym: Symbol = normalise_symbol(str(payload.underlying_symbol))
                        intent_ts: str = str(intent.intent_id).split(":")[-1]
                        client_order_id = ClientOrderId(f"PMCC:{underlying_sym}:{intent_ts}")

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
