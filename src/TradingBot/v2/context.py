from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from TradingBot.v2.domain.types import Symbol

@dataclass(frozen=True)
class RiskContext:
    """
    Immutable snapshot of account and market state for a single orchestration cycle.

    Design intent
    - This object represents a consistent, point-in-time view of the world.
    - It is constructed once per cycle by the orchestrator.
    - It is passed read-only to strategies and the risk engine.

    Why this exists
    - Strategies must not query the broker directly for account state.
    - Risk decisions must be made against a stable snapshot, not moving data.
    - Freezing this dataclass prevents accidental mutation during evaluation.

    What this is NOT
    - It is not a cache that updates itself.
    - It is not a live view of the account.
    - It should never contain broker methods or side effects.
    """

    # Timestamp marking when this snapshot was taken.
    # Useful for logging, debugging, and correlating decisions with market events.
    as_of_utc: datetime

    # Available option buying power at the time of snapshot.
    # Used by the risk engine to enforce allocation and sizing rules.
    option_buying_power: float

    # Total account equity or portfolio value.
    # This allows future risk rules such as max drawdown, daily loss limits,
    # or equity-based scaling.
    equity: float

    # Current open positions, keyed by symbol.
    # The exact structure is broker-dependent, so we keep values as Any.
    # Risk rules may inspect this to prevent duplicate positions or enforce caps.
    positions: Dict[str, Any]

    # List of open orders at the time of snapshot.
    # Used primarily for deduplication and order throttling.
    # This is intentionally a raw structure to avoid premature abstraction.
    open_orders: List[Dict[str, Any]]

    # Mapping of underlying symbol -> latest known price.
    # Prices are fetched once per cycle to avoid repeated broker calls.
    # This supports strike selection, sizing, and sanity checks.
    prices: Dict[Symbol, float]

    def get_price(self, symbol: Symbol) -> Optional[float]:
        """
        Retrieve the cached price for a given symbol.

        Behaviour
        - Symbol lookup is normalised to uppercase.
        - Returns None if the symbol was not included in the snapshot.

        Why this helper exists
        - Centralises symbol normalisation.
        - Avoids scattered `.upper()` calls across strategies and risk code.
        - Makes missing-price handling explicit.
        """
        return self.prices.get(symbol)



