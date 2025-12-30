from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2
from TradingBot.v2.logger import setup_logger
logger = setup_logger("AlpacaBroker")


@dataclass
class AlpacaBrokerV2(BrokerInterfaceV2):
    """
    Alpaca broker adapter for V2 Option C.

    Core rule
    - No network IO in __init__ or __post_init__.
      The broker object must be cheap to construct.
      All API calls happen inside explicit methods called by the orchestrator.

    Why this exists
    - V2 must not import V1 code.
    - V2 must be testable by swapping in FakeBrokerV2.
    - The orchestrator must be the only component that performs broker IO.

    Implementation notes
    - This uses the official alpaca-py SDK.
    - We lazily instantiate SDK clients the first time they are needed.
    """

    api_key: str
    api_secret: str
    paper: bool = True

    # A conservative timeout protects you against hanging sockets.
    request_timeout_seconds: float = 10.0

    # Cached SDK clients. These are created lazily on first use.
    _trading_client: Optional[Any] = field(default=None, init=False)

    def _base_url(self) -> str:
        """
        Decide which Alpaca endpoint to use.

        Why this exists
        - Alpaca has separate endpoints for paper and live trading.
        - We keep this logic in one place so it is hard to misconfigure.
        """
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    def _ensure_trading_client(self) -> Any:
        """
        Lazily create the alpaca-py TradingClient.

        Why lazy creation matters
        - Creating a client object is cheap and should not call the network.
        - We avoid importing alpaca-py at module import time if you are running tests
          without Alpaca installed.
        """
        if self._trading_client is None:
            # Local import reduces import side effects and keeps tests flexible.
            from alpaca.trading.client import TradingClient

            # TradingClient construction should not perform network IO.
            self._trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=self.paper,
            )
        return self._trading_client

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        """
        Convert a value to float safely.

        Why this exists
        - Alpaca SDK objects may provide values as strings or decimals.
        - The rest of the system expects floats for arithmetic.
        """
        try:
            return float(value)
        except Exception:
            return float(default)

    def get_option_buying_power(self) -> float:
        """
        Return option buying power from Alpaca.

        What this calls
        - GET /account via TradingClient.get_account()

        Important
        - This is network IO.
        - Only the orchestrator should call this during a cycle.
        """
        client: Any = self._ensure_trading_client()

        # Network IO occurs here.
        account: Any = client.get_account()

        # Alpaca exposes buying power fields on the account object.
        # Options buying power availability can differ by account and permissions.
        # We attempt a few common fields in a safe order.
        candidates: List[Any] = [
            getattr(account, "options_buying_power", None),
            getattr(account, "buying_power", None),
            getattr(account, "cash", None),
        ]

        for c in candidates:
            if c is None:
                continue
            val: float = self._safe_float(c, default=0.0)
            if val > 0.0:
                return val

        return 0.0

    def get_equity(self) -> float:
        """
        Return account equity from Alpaca.

        What this calls
        - GET /account via TradingClient.get_account()

        Why this matters
        - Equity is used for risk controls and reporting.
        """
        client: Any = self._ensure_trading_client()

        # Network IO occurs here.
        account: Any = client.get_account()

        equity_raw: Any = getattr(account, "equity", None)
        equity: float = self._safe_float(equity_raw, default=0.0)

        return max(equity, 0.0)

    def get_positions(self) -> Dict[str, Any]:
        """
        Return positions from Alpaca.

        What this calls
        - GET /positions via TradingClient.get_all_positions()

        Return shape
        - A dict keyed by symbol, values are raw Alpaca position objects converted to dict-like payloads
          where possible.

        Why we keep Any
        - We will later define typed V2 position models.
        - For now, RiskContext stores a broker snapshot and rules can inspect it defensively.
        """
        client: Any = self._ensure_trading_client()

        # Network IO occurs here.
        positions: Any = client.get_all_positions()

        result: Dict[str, Any] = {}

        if not positions:
            return result

        for pos in positions:
            sym: str = str(getattr(pos, "symbol", "")).strip().upper()
            if not sym:
                continue

            # Keep raw object, but also try to provide a plain dict if the SDK supports it.
            payload: Any = pos
            if hasattr(pos, "model_dump"):
                try:
                    payload = pos.model_dump()
                except Exception:
                    payload = pos

            result[sym] = payload

        return result

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return open orders from Alpaca.

        What this calls
        - GET /orders via TradingClient.get_orders()

        Why this matters
        - Open-order deduplication is one of the first risk controls in Option C.
        """
        client: Any = self._ensure_trading_client()

        # Local import to avoid importing request models when running tests that do not need Alpaca.
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)

        # Network IO occurs here.
        orders: Any = client.get_orders(request)

        results: List[Dict[str, Any]] = []

        if not orders:
            return results

        for o in orders:
            if hasattr(o, "model_dump"):
                try:
                    results.append(o.model_dump())
                    continue
                except Exception:
                    pass

            # Minimal fallback representation.
            results.append(
                {
                    "id": getattr(o, "id", None),
                    "client_order_id": getattr(o, "client_order_id", None),
                    "symbol": getattr(o, "symbol", None),
                    "status": getattr(o, "status", None),
                    "legs": getattr(o, "legs", None),
                }
            )

        return results

    def get_asset_price(self, symbol: str) -> float:
        """
        Return the current market price for an underlying.

        Important
        - Alpaca's trading client does not always provide last-trade prices.
        - Quote/market-data access depends on your subscription and environment.
        - For V2, this method must work reliably, so we implement it via requests to Alpaca's data API.

        If your account lacks data permissions
        - This may fail.
        - In that case, the orchestrator should handle exceptions and omit the price.

        Why we use requests here
        - alpaca-py has data clients, but they have changed between versions.
        - A direct REST call keeps behaviour explicit and debuggable.
        """
        import requests

        sym: str = symbol.strip().upper()
        if not sym:
            raise ValueError("symbol must be a non-empty string")

        url: str = f"https://data.alpaca.markets/v2/stocks/{sym}/quotes/latest"
        headers: Dict[str, str] = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

        # Network IO occurs here.
        resp = requests.get(url, headers=headers, timeout=self.request_timeout_seconds)
        resp.raise_for_status()

        data: Dict[str, Any] = resp.json()

        # Alpaca latest quote returns {"quote": {"ap": ask_price, "bp": bid_price, ...}}
        quote: Any = data.get("quote", {})
        ask: float = self._safe_float(quote.get("ap"), default=0.0)
        bid: float = self._safe_float(quote.get("bp"), default=0.0)

        # Mid price is a reasonable approximation for strategy selection.
        if ask > 0.0 and bid > 0.0:
            return (ask + bid) / 2.0

        # Fallback: if only one side exists, return it.
        if ask > 0.0:
            return ask
        if bid > 0.0:
            return bid

        raise RuntimeError(f"Alpaca returned no usable quote for {sym}: {data}")

    def submit_order(self, order: Any) -> Any:
        """
        Submit an order to Alpaca.

        Important
        - This is network IO.
        - Only the orchestrator is allowed to call this.
        - In Option C, the orchestrator will submit only ApprovedOrder objects.

        For now
        - We raise because V2 order models are still being finalised in v2/domain/orders.py.
        - Once your V2 order request type is defined, this method will translate it into Alpaca order requests.
        """
        raise NotImplementedError(
            "Order submission not implemented yet. "
            "Define v2/domain/orders.py request types first, then map them here."
        )
