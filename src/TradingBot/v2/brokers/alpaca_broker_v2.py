from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2


@dataclass
class AlpacaBrokerV2(BrokerInterfaceV2):
    """
    Alpaca broker adapter for V2 Option C.

    Why this exists
    - The V1 Alpaca broker performs network IO during construction, which can hang and blocks dry-run.
    - Option C requires that object construction is cheap and side-effect free.
    - Network IO must occur only when explicitly requested by orchestrator methods.

    Design rules
    - __init__ and __post_init__ must not call the Alpaca API.
    - All IO happens inside methods like get_equity(), get_open_orders(), etc.
    - IO errors should surface clearly, but not crash the whole process without context.
    """

    api_key: str
    api_secret: str
    paper: bool = True

    request_timeout_seconds: float = 10.0

    _v1_broker: Optional[Any] = field(default=None, init=False)

    def _ensure_client(self) -> Any:
        """
        Lazily create the underlying V1 broker object.

        Why lazy initialisation
        - It prevents network IO at import time or object construction time.
        - It keeps startup deterministic and test-friendly.

        Important note
        - This assumes the V1 AlpacaBroker can be constructed without IO.
        - If V1 AlpacaBroker still performs IO in __post_init__, then this method will still block.
          In that case, the correct fix is to refactor V1 AlpacaBroker to remove IO in __post_init__.
        """
        if self._v1_broker is None:
            from TradingBot.brokers.alpaca_broker import AlpacaBroker  # local import avoids eager side effects

            # If your V1 AlpacaBroker currently calls get_account in __post_init__, that must be removed or guarded.
            # For V2, we need an Alpaca broker that does not call the network in constructors.
            self._v1_broker = AlpacaBroker(
                api_key=self.api_key,
                api_secret=self.api_secret,
                paper=self.paper,
            )
        return self._v1_broker

    def get_option_buying_power(self) -> float:
        """
        Return option buying power from Alpaca.

        Why this method exists
        - The orchestrator builds RiskContext from it.
        - The risk engine uses it for sizing and allocation later.
        """
        broker: Any = self._ensure_client()
        value: Any = broker.get_option_buying_power()
        return float(value)

    def get_equity(self) -> float:
        """
        Return account equity from Alpaca.

        Why this method exists
        - Equity is a core risk control input.
        """
        broker: Any = self._ensure_client()
        value: Any = broker.get_equity()
        return float(value)

    def get_positions(self) -> Dict[str, Any]:
        """
        Return current positions.

        Why dict
        - Positions are easier to query by symbol when keyed.
        """
        broker: Any = self._ensure_client()
        value: Any = broker.get_positions()
        if not isinstance(value, dict):
            return {}
        return value

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return currently open orders.

        Why list
        - Deduplication rules scan open orders as records.
        """
        broker: Any = self._ensure_client()
        value: Any = broker.get_open_orders()
        if not isinstance(value, list):
            return []
        return value

    def get_asset_price(self, symbol: str) -> float:
        """
        Return current asset price.

        Why this method exists
        - V2 strategies use ctx.get_price(symbol), which is populated by the orchestrator.
        """
        broker: Any = self._ensure_client()
        value: Any = broker.get_asset_price(symbol)
        return float(value)

    def submit_order(self, order: Any) -> Any:
        """
        Submit an order.

        Important
        - V2 should only submit through the orchestrator, and only when dry_run is False.
        - Until submission is implemented in orchestrator, calling this is a design violation.
        """
        broker: Any = self._ensure_client()
        return broker.submit_order(order)


