from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Optional

from TradingBot.domain.intents import PmccIntentPayload, TradeIntent
from TradingBot.domain.types import ClientOrderId, Symbol, normalise_symbol
from TradingBot.execution.order_specs import PmccOrderSpec
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.risk.decisions import ApprovedIntent, RejectedIntent, RiskDecision
from TradingBot.risk.pmcc_sizer import PmccSizer, PmccSizingResult
from TradingBot.risk.rules.risk_rule_interface import RiskRuleABC, RiskRuleOutcome
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("RiskEngine")


class RiskEngineABC(ABC):
    @abstractmethod
    def evaluate(self, snapshot: CycleSnapshotABC, intents: List[TradeIntent]) -> List[RiskDecision]:
        raise NotImplementedError


@dataclass(frozen=True)
class RiskEngine(RiskEngineABC):
    """
    Central risk evaluation engine.

    Contract (UML)
    - Applies rules per intent.
    - Sizes intents as needed.
    - Outputs ApprovedIntent(spec=...) without constructing domain orders.
    - Contains no broker IO.
    """

    rules: List[RiskRuleABC]
    pmcc_sizer: PmccSizer
    pmcc_max_units_per_underlying: int = 2

    def _reject(self, intent: TradeIntent, reason: str) -> RiskDecision:
        rejected = RejectedIntent(
            intent_id=str(intent.intent_id),
            strategy_id=str(intent.strategy_id),
            symbol=str(intent.symbol),
            reason=reason,
        )
        return RiskDecision(rejected=rejected)

    @staticmethod
    def _safe_str(v: Any) -> str:
        return "" if v is None else str(v)

    def _apply_rules_one_intent(self, snapshot: CycleSnapshotABC, intent: TradeIntent) -> RiskRuleOutcome:
        for rule in self.rules:
            outcome: RiskRuleOutcome = rule.apply(snapshot, intent)
            if not outcome.allowed:
                return outcome
        return RiskRuleOutcome.allow()

    def _count_pmcc_units_from_positions(self, *, snapshot: CycleSnapshotABC, underlying: Symbol) -> int:
        long_calls: int = 0
        short_calls: int = 0

        for pos in snapshot.positions():
            if not isinstance(pos, dict):
                continue

            sym: str = self._safe_str(pos.get("symbol")).strip().upper()
            qty_raw: Any = pos.get("qty") if "qty" in pos else pos.get("quantity")
            try:
                qty: float = float(qty_raw) if qty_raw is not None else 0.0
            except Exception:
                qty = 0.0

            if str(underlying).strip().upper() not in sym:
                continue
            if "C" not in sym:
                continue

            if qty > 0:
                long_calls += 1
            elif qty < 0:
                short_calls += 1

        return int(min(long_calls, short_calls))

    def _build_pmcc_client_order_id(self, *, underlying: Symbol, intent: TradeIntent) -> ClientOrderId:
        intent_tail: str = str(intent.intent_id).split(":")[-1]
        return ClientOrderId(f"PMCC:{underlying}:{intent_tail}")

    def evaluate(self, snapshot: CycleSnapshotABC, intents: List[TradeIntent]) -> List[RiskDecision]:
        decisions: List[RiskDecision] = []

        with log_scope("risk_engine.evaluate", logger, extra=f"intents={len(intents)}"):
            for intent in intents:
                rule_outcome: RiskRuleOutcome = self._apply_rules_one_intent(snapshot, intent)
                if not rule_outcome.allowed:
                    decisions.append(self._reject(intent=intent, reason=str(rule_outcome.reason)))
                    continue

                payload: Any = intent.payload

                if isinstance(payload, PmccIntentPayload):
                    underlying_sym: Symbol = normalise_symbol(str(payload.underlying_symbol))

                    existing_units: int = self._count_pmcc_units_from_positions(snapshot=snapshot, underlying=underlying_sym)
                    if existing_units >= int(self.pmcc_max_units_per_underlying):
                        decisions.append(
                            self._reject(
                                intent=intent,
                                reason=f"PMCC unit cap reached | underlying={underlying_sym} cap={self.pmcc_max_units_per_underlying}",
                            )
                        )
                        continue

                    equity: float = float(snapshot.equity())
                    option_bp: float = float(snapshot.option_buying_power())

                    sizing: PmccSizingResult = self.pmcc_sizer.size(
                        equity=equity,
                        option_buying_power=option_bp,
                        leap=payload.leap_leg.contract,
                        near=payload.near_leg.contract,
                    )

                    if int(sizing.qty) <= 0:
                        decisions.append(self._reject(intent=intent, reason="Sizer produced non-positive quantity"))
                        continue

                    if float(sizing.limit_price) <= 0.0:
                        decisions.append(self._reject(intent=intent, reason="Sizer produced non-positive limit price"))
                        continue

                    client_order_id: ClientOrderId = self._build_pmcc_client_order_id(underlying=underlying_sym, intent=intent)

                    approval_payload = PmccOrderSpec(
                        underlying_symbol=underlying_sym,
                        leap_contract_symbol=str(payload.leap_leg.contract.option_symbol),
                        near_contract_symbol=str(payload.near_leg.contract.option_symbol),
                        quantity=int(sizing.qty),
                        limit_price=float(sizing.limit_price),
                        time_in_force=intent.time_in_force,
                    )

                    approved = ApprovedIntent(
                        intent_id=intent.intent_id,
                        strategy_id=intent.strategy_id,
                        symbol=intent.symbol,
                        client_order_id=client_order_id,
                        approval_payload=approval_payload
                    )
                    decisions.append(RiskDecision(approved=approved))
                    continue

                decisions.append(self._reject(intent=intent, reason="Unsupported intent payload type"))

        return decisions
