from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.domain.intents import (
    CloseLegPayload,
    CloseSpreadPayload,
    EnterPmccPayload,
    IntentPayloadABC,
    RollNearPayload,
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
    Handles all PMCC intent payload types.

    Dispatches internally on payload type:
      EnterPmccPayload   → size the spread, check unit cap + buying power
      RollNearPayload    → check net debit ceiling
      CloseLegPayload    → always approve (management, no sizing needed)
      CloseSpreadPayload → always approve (management, no sizing needed)

    A single evaluator for all PMCC actions because:
      - All four payloads are PMCC-specific management decisions.
      - The sizer and unit-cap logic are shared state (self.sizer, self.max_units).
      - There is no meaningful re-use of "single-leg close" logic outside PMCC context.
    """
    sizer:     PmccSizer
    max_units: int

    def evaluate(self, snapshot: CycleSnapshot, intent: TradeIntent) -> EvaluatorResult:
        payload = intent.payload

        if isinstance(payload, EnterPmccPayload):
            return self._evaluate_entry(snapshot, intent, payload)

        if isinstance(payload, RollNearPayload):
            return self._evaluate_roll(snapshot, intent, payload)

        if isinstance(payload, (CloseLegPayload, CloseSpreadPayload)):
            # Management closes are always approved — risk engine already gated
            # via rules. Evaluator does not second-guess a close decision.
            logger.info(
                "Close approved | id=%s type=%s symbol=%s",
                intent.intent_id, type(payload).__name__, intent.symbol,
            )
            return EvaluatorResult.approve(str(intent.intent_id), payload)

        return EvaluatorResult.reject(
            str(intent.intent_id),
            f"PmccEvaluator received unexpected payload type: {type(payload).__name__}",
        )

    # ------------------------------------------------------------------
    # Entry sizing
    # ------------------------------------------------------------------

    def _evaluate_entry(
        self,
        snapshot: CycleSnapshot,
        intent:   TradeIntent,
        payload:  EnterPmccPayload,
    ) -> EvaluatorResult:
        sym = str(intent.symbol).upper()

        # Unit cap — count existing long LEAP positions for this underlying.
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

        # Pull prices from the option chain snapshot.
        # We need leap ask (what we pay) and near bid (what we collect).
        leap_ask: Optional[float] = None
        near_bid: Optional[float] = None

        chains = snapshot.option_chains()
        for chain_data in chains.values():
            for row in chain_data:
                cs = str(row.get("contract_symbol", "")).upper()
                if cs == payload.leap.option_symbol.upper():
                    ask = row.get("latestQuote", {}).get("ap")
                    if ask:
                        leap_ask = float(ask)
                if cs == payload.near.option_symbol.upper():
                    bid = row.get("latestQuote", {}).get("bp")
                    if bid:
                        near_bid = float(bid)

        if leap_ask is None or near_bid is None:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                "Could not find prices for sizing",
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

        # Enforce the strategy's own max_debit ceiling.
        if net_debit > payload.max_debit:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                f"Net debit {net_debit:.2f} exceeds strategy ceiling {payload.max_debit:.2f}",
            )

        # Return the original payload — quantity lives in the order builder,
        # not in the payload. The execution policy calls the sizer again or
        # receives qty via the approved intent's payload extension.
        # For now we attach qty by rebuilding the payload with it.
        # (Alternative: add qty to EnterPmccPayload. We do that here.)
        approved_payload = _EnterPmccApproved(
            underlying_symbol=payload.underlying_symbol,
            leap=payload.leap,
            near=payload.near,
            max_debit=payload.max_debit,
            quantity=qty,
            limit_price=net_debit,
            time_in_force=intent.time_in_force,
        )

        logger.info(
            "PMCC entry approved | symbol=%s qty=%d debit=%.2f",
            sym, qty, net_debit,
        )
        return EvaluatorResult.approve(str(intent.intent_id), approved_payload)

    # ------------------------------------------------------------------
    # Roll net-debit check
    # ------------------------------------------------------------------

    def _evaluate_roll(
        self,
        snapshot: CycleSnapshot,
        intent:   TradeIntent,
        payload:  RollNearPayload,
    ) -> EvaluatorResult:
        sym = str(intent.symbol).upper()

        # Look up close bid and open ask from chain cache.
        close_bid: Optional[float] = None
        open_ask:  Optional[float] = None

        chains = snapshot.option_chains()
        for chain_data in chains.values():
            for row in chain_data:
                cs = str(row.get("contract_symbol", "")).upper()
                if cs == payload.close.option_symbol.upper():
                    bid = row.get("latestQuote", {}).get("bp")
                    if bid:
                        close_bid = float(bid)
                if cs == payload.open.option_symbol.upper():
                    ask = row.get("latestQuote", {}).get("ap")
                    if ask:
                        open_ask = float(ask)

        if close_bid is None or open_ask is None:
            # Prices unavailable — approve conservatively and let the broker
            # fill at market. A roll is a management action; refusing it due
            # to missing quotes is worse than allowing it.
            logger.warning(
                "Roll prices unavailable — approving at market | symbol=%s close=%s open=%s",
                sym, payload.close.option_symbol, payload.open.option_symbol,
            )
            return EvaluatorResult.approve(str(intent.intent_id), payload)

        # Net debit of the roll: we pay open_ask and collect close_bid.
        # Negative result means we collect a net credit (ideal).
        net_debit = open_ask - close_bid

        if net_debit > payload.max_net_debit:
            return EvaluatorResult.reject(
                str(intent.intent_id),
                f"Roll net debit {net_debit:.2f} exceeds ceiling {payload.max_net_debit:.2f} "
                f"| symbol={sym}",
            )

        logger.info(
            "Roll approved | symbol=%s close=%s open=%s net_debit=%.2f",
            sym, payload.close.option_symbol, payload.open.option_symbol, net_debit,
        )
        return EvaluatorResult.approve(str(intent.intent_id), payload)


# ---------------------------------------------------------------------------
# Internal approved payload — carries sizing result out of the evaluator
# without polluting the strategy-facing EnterPmccPayload with order details.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _EnterPmccApproved(IntentPayloadABC):
    """
    Approved PMCC entry — extends EnterPmccPayload with sizing output.

    This type only exists between evaluator → execution policy.
    Strategy and RiskEngine never see it.
    """
    from src.domain.orders import TimeInForce as _TIF
    from src.domain.types import Symbol as _Sym

    underlying_symbol: object   # Symbol
    leap:              SelectedOption
    near:              SelectedOption
    max_debit:         float
    quantity:          int
    limit_price:       float
    time_in_force:     object   # TimeInForce