# src/TradingBot/v2/brokers/fake_broker_v2.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2
from TradingBot.v2.domain.types import AssetQuote
from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope

logger = setup_logger("FakeBroker")


@dataclass
class FakeBrokerV2(BrokerInterfaceV2):
    """
    Deterministic, in-memory broker implementation for TradingBot V2.

    Why this exists
    - Unit tests should not depend on external network services.
    - Dry-run mode should be able to run the full pipeline end-to-end.
    - Strategies must never perform broker IO, so the orchestrator can be tested
      with predictable broker outputs.

    Design rules
    - No network IO, ever.
    - Every method returns a stable result based on preloaded state.
    """

    # Account state exposed via get_account_snapshot, get_equity, get_option_buying_power.
    equity: float = 100_000.0
    options_buying_power: float = 100_000.0
    cash: Optional[float] = None

    # Positions and orders are intentionally left as raw shapes until V2 models stabilise.
    positions: Dict[str, Any] = field(default_factory=dict)
    open_orders: List[Dict[str, Any]] = field(default_factory=list)

    # Latest quotes keyed by uppercase symbol, for example {"SPY": AssetQuote(...)}.
    asset_quotes: Dict[str, AssetQuote] = field(default_factory=dict)

    # Option contracts keyed by uppercase underlying symbol.
    # Each entry is a list of raw contract records (dicts) shaped like the broker response.
    option_chains: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    # Captured submissions for inspection in tests.
    submitted_orders: List[Any] = field(default_factory=list)

    # ---------------------------------------------------------------------
    # BrokerInterfaceV2 implementation
    # ---------------------------------------------------------------------

    def get_account_snapshot(self) -> AccountSnapshot:
        """
        Return a point-in-time snapshot of account state.

        Why this exists in FakeBroker
        - Tests can assert orchestrator behaviour without calling the real broker.
        - Orchestrator reads this once per cycle and pushes it into RiskContext.
        """
        self._log_io_boundary("get_account_snapshot")
        with log_scope("broker.get_account_snapshot", logger):
            logger.info("Fake broker get_account_snapshot")

            raw: Dict[str, Any] = {
                "equity": float(self.equity),
                "options_buying_power": float(self.options_buying_power),
                "cash": float(self.cash) if self.cash is not None else None,
            }

            return AccountSnapshot(
                equity=float(self.equity),
                options_buying_power=float(self.options_buying_power),
                cash=float(self.cash) if self.cash is not None else None,
                raw=raw,
            )

    def get_option_buying_power(self) -> float:
        """
        Return option buying power.

        Why this is separate from get_account_snapshot
        - Some callers may use the scalar method directly.
        - Keeping it supports existing orchestrator code patterns.
        """
        self._log_io_boundary("get_option_buying_power")
        with log_scope("broker.get_option_buying_power", logger):
            obp: float = max(float(self.options_buying_power), 0.0)
            logger.info("Fake broker get_option_buying_power options_buying_power=%.2f", float(obp))
            return obp

    def get_equity(self) -> float:
        """
        Return account equity.

        Why this is separate from get_account_snapshot
        - Consistent with the V2 interface.
        - Simplifies tests that only care about equity rules.
        """
        self._log_io_boundary("get_equity")
        with log_scope("broker.get_equity", logger):
            eq: float = max(float(self.equity), 0.0)
            logger.info("Fake broker get_equity equity=%.2f", float(eq))
            return eq

    def get_positions(self) -> Dict[str, Any]:
        """
        Return positions keyed by symbol.

        This is intentionally a shallow copy to reduce accidental mutation in tests.
        """
        self._log_io_boundary("get_positions")
        with log_scope("broker.get_positions", logger):
            logger.info("Fake broker get_positions count=%d", int(len(self.positions)))
            return dict(self.positions)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Return open orders.

        This is intentionally a shallow copy to reduce accidental mutation in tests.
        """
        self._log_io_boundary("get_open_orders")
        with log_scope("broker.get_open_orders", logger):
            logger.info("Fake broker get_open_orders count=%d", int(len(self.open_orders)))
            return list(self.open_orders)

    def get_asset_quote(self, symbol: str) -> AssetQuote:
        """
        Return the latest bid/ask quote for a symbol.

        Behaviour
        - If a quote exists in asset_quotes, return it.
        - If no quote exists, raise KeyError to expose missing test setup early.

        Why this is strict
        - Silent defaults hide bugs.
        - Strategies and orchestrator must handle missing data explicitly.
        """
        self._log_io_boundary("get_asset_quote")

        sym: str = symbol.strip().upper()
        if not sym:
            raise ValueError("symbol must be a non-empty string")

        with log_scope("broker.get_asset_quote", logger, extra=f"symbol={sym}"):
            logger.info("Fake broker get_asset_quote symbol=%s", sym)

            quote: Optional[AssetQuote] = self.asset_quotes.get(sym)
            if quote is None:
                logger.error("Fake broker missing quote | symbol=%s", sym)
                raise KeyError(f"No fake quote configured for symbol={sym}")

            # Returning the stored dataclass is safe if it is treated as immutable.
            logger.info(
                "Fake broker quote | symbol=%s bid=%s ask=%s mid=%s",
                sym,
                quote.bid,
                quote.ask,
                quote.mid,
            )
            return quote

    def get_option_chain(
        self,
        underlying_symbol: str,
        *,
        include_calls: bool = True,
        include_puts: bool = False,
        status: str = "active",
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Return option contracts for an underlying.

        This returns raw contract records, matching the V2 interface design.

        Filtering behaviour
        - include_calls/include_puts is applied using a best-effort type field lookup:
          "type", "right", or "option_type".
        - If type is missing, the contract is kept, and downstream selection can
          reject it if required.

        Notes
        - status and limit are accepted for compatibility with real brokers.
        - FakeBroker does not implement server-side pagination, but limit is applied
          to the final list to keep behaviour predictable.
        """
        self._log_io_boundary("get_option_chain")

        sym: str = underlying_symbol.strip().upper()
        if not sym:
            raise ValueError("underlying_symbol must be a non-empty string")

        if not include_calls and not include_puts:
            raise ValueError("At least one of include_calls or include_puts must be True")

        if limit <= 0:
            raise ValueError("limit must be a positive integer")

        with log_scope(
            "broker.get_option_chain",
            logger,
            extra=f"underlying={sym} include_calls={include_calls} include_puts={include_puts} status={status} limit={limit}",
        ):
            logger.info(
                "Fake broker get_option_chain | underlying=%s include_calls=%s include_puts=%s status=%s",
                sym,
                bool(include_calls),
                bool(include_puts),
                str(status),
            )

            raw_contracts: List[Dict[str, Any]] = list(self.option_chains.get(sym, []))

            filtered: List[Dict[str, Any]] = []
            for c in raw_contracts:
                if not isinstance(c, dict):
                    continue

                raw_type: str = str(
                    c.get("type") or c.get("right") or c.get("option_type") or ""
                ).strip().lower()

                if raw_type == "call" and include_calls:
                    filtered.append(c)
                    continue

                if raw_type == "put" and include_puts:
                    filtered.append(c)
                    continue

                # If we cannot identify the type, keep it.
                # This prevents tests from becoming brittle when fixtures vary.
                if raw_type == "":
                    filtered.append(c)

            # Apply the limit as a deterministic final truncation.
            limited: List[Dict[str, Any]] = filtered[: int(limit)]

            logger.info(
                "Fake broker option chain | underlying=%s raw=%d filtered=%d returned=%d",
                sym,
                int(len(raw_contracts)),
                int(len(filtered)),
                int(len(limited)),
            )

            return limited

    def submit_order(self, order: Any) -> Any:
        """
        Capture an order submission request.

        Behaviour
        - Stores the order in submitted_orders for later inspection.
        - Returns a lightweight acknowledgement object.

        Why this exists
        - Tests can assert that the orchestrator tried to submit the correct orders.
        - Dry-run can exercise the call path without talking to a broker.
        """
        self._log_io_boundary("submit_order")
        with log_scope("broker.submit_order", logger):
            logger.info("Fake broker submit_order captured | type=%s", type(order).__name__)
            self.submitted_orders.append(order)

            # This is a deliberately simple acknowledgement shape.
            # Real brokers will return order IDs and status fields.
            return {
                "status": "accepted",
                "broker": "fake",
                "index": int(len(self.submitted_orders) - 1),
                "order": order,
            }
