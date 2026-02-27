from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.domain.intents import (
    IntentPayloadABC,
    PositionIntent,
    RollPayload,
    SingleLegPayload,
    SpreadPayload,
    TradeIntent,
)
from src.orchestration.cycle_snapshot import CycleSnapshot
from src.risk.decisions import ApprovedIntent, RejectedIntent
from src.risk.pmcc_sizer import PmccSizer
from src.utilities.logger import setup_logger

logger = setup_logger("IntentEvaluators")


class IntentEvaluatorABC(ABC):
    @abstractmethod
    def evaluate(
        self,
        snapshot: CycleSnapshot,
        intent:   TradeIntent,
    ) -> "EvaluatorResult":
        raise NotImplementedError


@dataclass(frozen=True)
class EvaluatorResult:
    approved: Optional[ApprovedIntent]
    rejected: Optional[RejectedIntent]

    @classmethod
    def approve(cls, intent_id: str, payload: IntentPayloadABC) -> "EvaluatorResult":
        return cls(
            approved=ApprovedIntent(intent_id=intent_id, payload=payload),
            rejected=None,
        )

    @classmethod
    def reject(cls, intent_id: str, reason: str) -> "EvaluatorResult":
        return cls(
            approved=None,
            rejected=RejectedIntent(intent_id=intent_id, reason=reason),
        )


@dataclass
class PmccEvaluator(IntentEvaluatorABC):
    """
    Risk evaluator for the PMCC strategy.

    Handles three generic payload types:

      SpreadPayload with BTO leg  → size the entry (unit cap + buying power)
      SpreadPayload with BTC/STC  → close, always approve
      RollPayload                 → check net debit ceiling
      SingleLegPayload            → management close/open, always approve

    Sizing result is written back to SpreadPayload via dataclasses.replace() —
    no separate ApprovedSpreadPayload subclass needed.
    """
    sizer:     PmccSizer
    max_units: int

    def evaluate(self, snapshot: CycleSnapshot, intent: TradeIntent) -> EvaluatorResult:
        payload = intent.payload

        if isinstance(payload, SpreadPayload):
            # BTO in either leg means this is an entry that needs sizing.
            # BTC/STC only means it's a close — approve immediately.
            is_entry = any(
                leg.position_intent == PositionIntent.BUY_TO_OPEN
                for leg in (payload.leg_a, payload.leg_b)
            )
            return (
                self._evaluate_spread_entry(snapshot, intent, payload)
                if is_entry
                else self._approve_close(intent, payload)
            )

        if isinstance(payload, RollPayload):
            return self._evaluate_roll(snapshot, intent, payload)

        if isinstance(payload, SingleLegPayload):
            # Single-leg management actions (BTC, STC, STO, BTO individual legs)
            # are always approved — the strategy has already decided to act.
            logger.info(
                "Single-leg approved | id=%s role=%s intent=%s symbol=%s",
                intent.intent_id,
                payload.leg.role,
                payload.leg.position_intent.value,
                payload.leg.contract.option_symbol,
            )
            return EvaluatorResult.approve(str(intent.intent_id), payload)

        return EvaluatorResult.reject(
            str(intent.intent_id),
            f"PmccEvaluator received unhandled payload type: {type(payload).__name__}",
        )

    # ------------------------------------------------------------------
    # Spread entry sizing
    # ------------------------------------------------------------------

    def _evaluate_spread_entry(
        self,
        snapshot: CycleSnapshot,
        intent:   TradeIntent,
        payload:  SpreadPayload,
    ) -> EvaluatorResult:
        sym = str(intent.symbol).upper()

        # Unit cap
        positions  = snapshot.positions()
        unit_count = sum(
            1 for p in positions
            if str(p.get("underlying_symbol", "")).upper() == sym
            and p.get("asset_class") == "us_option"
            and float(p.get("qty") or 0) > 0
        )
        if unit_count >= self.max_units:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                f"Max PMCC units ({self.max_units}) already open for {sym}",
            )

        # Find the BTO leg (leap) and STO leg (near) from the payload
        bto_leg = next(
            (l for l in (payload.leg_a, payload.leg_b)
             if l.position_intent == PositionIntent.BUY_TO_OPEN), None
        )
        sto_leg = next(
            (l for l in (payload.leg_a, payload.leg_b)
             if l.position_intent == PositionIntent.SELL_TO_OPEN), None
        )
        if bto_leg is None or sto_leg is None:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                "SpreadPayload entry missing BTO or STO leg",
            )

        # Pull prices from option chain snapshot
        leap_ask: Optional[float] = None
        near_bid: Optional[float] = None

        chains = snapshot.option_chains
        for chain_data in chains.values():
            for row in chain_data:
                cs = str(row.get("contract_symbol", "")).upper()
                if cs == bto_leg.contract.option_symbol.upper():
                    ask = row.get("latestQuote", {}).get("ap")
                    if ask:
                        leap_ask = float(ask)
                if cs == sto_leg.contract.option_symbol.upper():
                    bid = row.get("latestQuote", {}).get("bp")
                    if bid:
                        near_bid = float(bid)

        if leap_ask is None or near_bid is None:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                "Could not find prices for spread sizing",
            )

        buying_power = snapshot.option_buying_power()

        logger.info(
            "Sizing | symbol=%s buying_power=%.2f leap_ask=%.2f near_bid=%.2f "
            "net_debit_per_contract=%.2f",
            sym, buying_power, leap_ask, near_bid, (leap_ask - near_bid) * 100,
        )

        qty = self.sizer.size(
            buying_power=buying_power,
            leap_ask=leap_ask,
            near_bid=near_bid,
        )
        if qty is None or qty < 1:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                "Sizer returned 0 — insufficient buying power",
            )

        net_debit = float(Decimal(str(leap_ask - near_bid)).quantize(Decimal("0.01")))

        if payload.max_debit > 0 and net_debit > payload.max_debit:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                f"Net debit {net_debit:.2f} exceeds strategy ceiling {payload.max_debit:.2f}",
            )

        # Write sizing result back onto the payload — no separate approved type needed
        sized_payload = dataclasses.replace(
            payload,
            quantity=qty,
            limit_price=net_debit,
        )

        logger.info(
            "Spread entry approved | symbol=%s qty=%d debit=%.2f",
            sym, qty, net_debit,
        )
        return EvaluatorResult.approve(str(intent.intent_id), sized_payload)

    def _approve_close(self, intent: TradeIntent, payload: SpreadPayload) -> EvaluatorResult:
        logger.info(
            "Spread close approved | id=%s symbol=%s",
            intent.intent_id, intent.symbol,
        )
        return EvaluatorResult.approve(str(intent.intent_id), payload)

    # ------------------------------------------------------------------
    # Roll net-debit check
    # ------------------------------------------------------------------

    def _evaluate_roll(
        self,
        snapshot: CycleSnapshot,
        intent:   TradeIntent,
        payload:  RollPayload,
    ) -> EvaluatorResult:
        sym = str(intent.symbol).upper()

        close_bid: Optional[float] = None
        open_ask:  Optional[float] = None

        chains = snapshot.option_chains
        for chain_data in chains.values():
            for row in chain_data:
                cs = str(row.get("contract_symbol", "")).upper()
                if cs == payload.close.contract.option_symbol.upper():
                    bid = row.get("latestQuote", {}).get("bp")
                    if bid:
                        close_bid = float(bid)
                if cs == payload.open_.contract.option_symbol.upper():
                    ask = row.get("latestQuote", {}).get("ap")
                    if ask:
                        open_ask = float(ask)

        if close_bid is None or open_ask is None:
            logger.warning(
                "Roll prices unavailable — approving at market | symbol=%s",
                sym,
            )
            return EvaluatorResult.approve(str(intent.intent_id), payload)

        net_debit = open_ask - close_bid

        if payload.max_net_debit > 0 and net_debit > payload.max_net_debit:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                f"Roll net debit {net_debit:.2f} exceeds ceiling "
                f"{payload.max_net_debit:.2f} | symbol={sym}",
            )

        logger.info(
            "Roll approved | symbol=%s close=%s open=%s net_debit=%.2f",
            sym,
            payload.close.contract.option_symbol,
            payload.open_.contract.option_symbol,
            net_debit,
        )
        return EvaluatorResult.approve(str(intent.intent_id), payload)