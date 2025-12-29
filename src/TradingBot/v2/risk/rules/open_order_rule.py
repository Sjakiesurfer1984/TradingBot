from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from TradingBot.v2.context import RiskContext
from TradingBot.v2.intents import TradeIntent


@dataclass(frozen=True)
class OpenOrderDedupeRule:
    """
    Reject intents if an open order already exists
    for the same underlying.
    """

    client_order_prefix: str = "PMCC"

    @staticmethod
    def _safe_str(value: Any) -> str:
        return "" if value is None else str(value)

    def _expected_client_order_id(self, symbol: str) -> str:
        return f"{self.client_order_prefix}:{symbol.strip().upper()}"

    def _mentions_underlying(self, underlying: str, order: Dict[str, Any]) -> bool:
        ul = underlying.strip().upper()

        symbol_field = self._safe_str(order.get("symbol")).upper()
        if symbol_field.startswith(ul):
            return True

        legs = order.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                leg_symbol = self._safe_str(leg.get("symbol")).upper()
                if leg_symbol.startswith(ul):
                    return True

        return False

    def check(self, ctx: RiskContext, intent: TradeIntent) -> Optional[str]:
        expected_id = self._expected_client_order_id(intent.symbol)

        for order in ctx.open_orders:
            if not isinstance(order, dict):
                continue

            client_order_id = self._safe_str(order.get("client_order_id"))
            if client_order_id == expected_id:
                return f"Open order exists for {intent.symbol} (client_order_id match)."

            if self._mentions_underlying(intent.symbol, order):
                return f"Open order exists for {intent.symbol} (symbol match)."

        return None
