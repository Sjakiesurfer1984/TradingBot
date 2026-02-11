from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from TradingBot.domain.intents import PmccIntentPayload, TradeIntent
from TradingBot.domain.types import ClientOrderId, Symbol
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.risk.rules.risk_rule_interface import RiskRuleABC, RiskRuleOutcome
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("Open Order Rule")


@dataclass(frozen=True)
class OpenOrderDedupeRule(RiskRuleABC):
    """
    Risk rule: reject intents if an open order already exists for the same underlying.

    Design intent
    - Prevent duplicate orders for the same underlying within overlapping cycles.
    - Strategies are intentionally unaware of global order state.
    """

    client_order_prefix: str = "PMCC"

    @staticmethod
    def _safe_str(value: Any) -> str:
        return "" if value is None else str(value)

    def _expected_client_order_id(self, underlying: Symbol) -> ClientOrderId:
        expected_raw: str = f"{self.client_order_prefix}:{str(underlying).strip().upper()}"
        return ClientOrderId(expected_raw)

    def _mentions_underlying(self, underlying: Symbol, order: Dict[str, Any]) -> bool:
        """
        Heuristic check: does the broker order payload mention the underlying.

        This is intentionally defensive because broker payload shapes are not stable.
        """
        underlying_u: str = str(underlying).strip().upper()

        symbol_raw: str = self._safe_str(order.get("symbol")).strip().upper()
        if symbol_raw == underlying_u:
            return True

        legs: Any = order.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                leg_sym: str = self._safe_str(leg.get("symbol")).strip().upper()
                if underlying_u in leg_sym:
                    return True

        return False

    def _extract_underlying(self, intent: TradeIntent) -> Symbol:
        payload: Any = intent.payload

        if isinstance(payload, PmccIntentPayload):
            return Symbol(str(payload.underlying_symbol).strip().upper())

        return Symbol(str(intent.symbol).strip().upper())

    def apply(self, snapshot: CycleSnapshotABC, intent: TradeIntent) -> RiskRuleOutcome:
        """
        Evaluate this rule for a single trade intent.
        """
        with log_scope(
            "open_order_rule.apply",
            logger,
            extra=f"intent_id={intent.intent_id} strategy_id={intent.strategy_id} symbol={intent.symbol}",
        ):
            underlying: Symbol = self._extract_underlying(intent)
            expected_id: ClientOrderId = self._expected_client_order_id(underlying)

            for order in snapshot.open_orders():
                if not isinstance(order, dict):
                    continue

                client_order_id_raw: str = self._safe_str(order.get("client_order_id")).strip().upper()
                if client_order_id_raw == str(expected_id).strip().upper():
                    return RiskRuleOutcome.reject(
                        f"Open order already exists for underlying={underlying} (client_order_id match)"
                    )

                if self._mentions_underlying(underlying, order):
                    return RiskRuleOutcome.reject(
                        f"Open order already exists for underlying={underlying} (heuristic match)"
                    )

            return RiskRuleOutcome.allow()

    def check(self, snapshot: CycleSnapshotABC, intent: TradeIntent) -> str | None:
        """
        Backwards-compatible wrapper.

        Return value
        - None: not rejected
        - str: rejected reason
        """
        outcome: RiskRuleOutcome = self.apply(snapshot, intent)
        return None if outcome.allowed else outcome.reason
