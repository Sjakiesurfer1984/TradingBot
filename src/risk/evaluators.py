from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.domain.intents import (
    OptionIntentPayload,
    PmccIntentPayload,
    TradeIntent,
)
from src.execution.order_specs import OptionMarketOrderSpec, PmccOrderSpec
from src.orchestration.cycle_snapshot import CycleSnapshotABC
from src.risk.decisions import ApprovedIntent, RejectedIntent
from src.risk.pmcc_sizer import PmccSizer
from src.utilities.logger import setup_logger

logger = setup_logger("IntentEvaluators")


class IntentEvaluatorABC(ABC):
    @abstractmethod
    def evaluate(
        self,
        snapshot: CycleSnapshotABC,
        intent:   TradeIntent,
    ) -> "EvaluatorResult":
        raise NotImplementedError


@dataclass(frozen=True)
class EvaluatorResult:
    approved: Optional[ApprovedIntent]
    rejected: Optional[RejectedIntent]

    @classmethod
    def approve(cls, intent_id: str, payload) -> "EvaluatorResult":
        return cls(
            approved=ApprovedIntent(intent_id=intent_id, approval_payload=payload),
            rejected=None,
        )

    @classmethod
    def reject(cls, intent_id: str, reason: str) -> "EvaluatorResult":
        return cls(
            approved=None,
            rejected=RejectedIntent(intent_id=intent_id, reason=reason),
        )


@dataclass
class PmccIntentEvaluator(IntentEvaluatorABC):
    sizer:     PmccSizer
    max_units: int

    def evaluate(self, snapshot: CycleSnapshotABC, intent: TradeIntent) -> EvaluatorResult:
        payload = intent.payload
        if not isinstance(payload, PmccIntentPayload):
            return EvaluatorResult.reject(str(intent.intent_id), "Wrong payload type for PmccIntentEvaluator")

        # Count existing PMCC units for this underlying
        sym        = str(intent.symbol).upper()
        positions  = snapshot.positions()
        unit_count = sum(
            1 for p in positions
            if str(p.get("underlying_symbol", "")).upper() == sym
            and p.get("asset_class") == "us_option"
            and str(p.get("position_intent", "")).upper() == "BTO"
        )

        if unit_count >= self.max_units:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                f"Max PMCC units ({self.max_units}) already open for {sym}",
            )

        # Get quotes for sizing
        quotes = snapshot.asset_quotes()
        sym_key = next((k for k in quotes if str(k).upper() == sym), None)
        equity  = snapshot.equity()
        opt_bp  = snapshot.option_buying_power()

        # Rough sizing: use mid prices from chain if available, otherwise skip
        leap_ask: Optional[float] = None
        near_bid: Optional[float] = None

        chains = snapshot.option_chains()
        for chain_data in chains.values():
            for row in chain_data:
                cs = str(row.get("contract_symbol", "")).upper()
                if cs == payload.leap_leg.option_symbol.upper():
                    ask = row.get("latestQuote", {}).get("ap")
                    if ask:
                        leap_ask = float(ask)
                if cs == payload.near_leg.option_symbol.upper():
                    bid = row.get("latestQuote", {}).get("bp")
                    if bid:
                        near_bid = float(bid)

        if leap_ask is None or near_bid is None:
            return EvaluatorResult.reject(str(intent.intent_id), "Could not find prices for sizing")

        qty = self.sizer.size(
            equity=equity,
            option_buying_power=opt_bp,
            leap_ask=leap_ask,
            near_bid=near_bid,
        )

        if qty is None or qty < 1:
            return EvaluatorResult.reject(str(intent.intent_id), "Sizer returned 0 — insufficient capital")

        net_debit = Decimal(str(leap_ask - near_bid)).quantize(Decimal("0.01"))

        spec = PmccOrderSpec(
            underlying=intent.symbol,
            leap=payload.leap_leg,
            near=payload.near_leg,
            quantity=qty,
            limit_price=net_debit,
            time_in_force=intent.time_in_force,
        )

        logger.info("PMCC approved | symbol=%s qty=%d debit=%.2f", sym, qty, net_debit)
        return EvaluatorResult.approve(str(intent.intent_id), spec)


@dataclass
class OptionIntentEvaluator(IntentEvaluatorABC):

    def evaluate(self, snapshot: CycleSnapshotABC, intent: TradeIntent) -> EvaluatorResult:
        payload = intent.payload
        if not isinstance(payload, OptionIntentPayload):
            return EvaluatorResult.reject(str(intent.intent_id), "Wrong payload type for OptionIntentEvaluator")

        leg = payload.leg
        qty = leg.qty if leg.qty is not None and leg.qty > 0 else 1

        spec = OptionMarketOrderSpec(
            underlying=intent.symbol,
            option_symbol=leg.contract_symbol,
            quantity=qty,
            position_intent=leg.position_intent.value,
            time_in_force=intent.time_in_force,
        )

        logger.info(
            "Option approved | symbol=%s intent=%s qty=%d",
            intent.symbol, leg.position_intent.value, qty,
        )
        return EvaluatorResult.approve(str(intent.intent_id), spec)
