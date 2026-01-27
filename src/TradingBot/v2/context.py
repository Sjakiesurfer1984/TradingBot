from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from TradingBot.v2.domain.types import AssetQuote, Symbol
from TradingBot.v2.risk.price_policy import PriceSelectionPolicy, PriceSide

@dataclass(frozen=True)
class RiskContext:
    """
    Immutable snapshot of account and market state for a single orchestration cycle.

    Design intent
    - Constructed once per cycle by the orchestrator.
    - Passed read-only to strategies and the risk engine.
    - Contains a consistent, point-in-time view of:
        - account state (equity, buying power, open orders, positions),
        - underlying quotes (stocks/ETFs),
        - option quotes (option contracts).

    Important terminology note
    - In our code, "snapshot" refers to this whole RiskContext object.
    - In Alpaca docs, "snapshot" can also refer to a market-data endpoint that
      returns bundled data (quote/trade/bar) for a symbol or option contract.
      We keep these concepts separate by naming our fields explicitly.
    """

    # Timestamp marking when this snapshot was taken.
    as_of_utc: datetime

    # Available option buying power at the time of snapshot.
    option_buying_power: float

    # Total account equity at the time of snapshot.
    equity: float

    # Open orders at the time of snapshot (raw broker payloads).
    open_orders: List[Dict[str, Any]]

    # Positions at the time of snapshot (raw broker payloads).
    positions: List[Dict[str, Any]]

    # Underlying symbol -> latest quote (bid/ask/mid).
    # Populated by orchestrator using broker market-data IO once per cycle.
    asset_quotes: Dict[Symbol, AssetQuote]

    price_policy: PriceSelectionPolicy

    # optional, because not all strategies need it
    option_quotes: Dict[str, Any] = field(default_factory=dict)

    option_chains: Dict[tuple[Symbol, str], List[Dict[str, Any]]] = field(default_factory=dict)

    def get_asset_quote(self, symbol: Symbol) -> AssetQuote:
        """
        Retrieve the cached underlying quote for a given symbol.

        Behaviour
        - Raises KeyError if missing, because that indicates orchestrator wiring
          or symbol declaration errors.

        Why this exists
        - Strategies must not call the broker directly.
        - Centralises access to underlying quote data.
        """
        if symbol not in self.asset_quotes:
            raise KeyError(f"Missing quote for symbol={symbol}")
        return self.asset_quotes[symbol]

    def get_execution_price(self, symbol: Symbol, side: PriceSide) -> float:
        """
        Retrieve an execution-aware price for an underlying symbol and trade side.

        Behaviour
        - Delegates bid/ask/mid selection to the injected PriceSelectionPolicy.
        - Raises ValueError if no safe execution price can be determined.

        Why this exists
        - Strategies must not hardcode bid/ask logic.
        - Pricing rules must be explicit, testable, and replaceable.
        """
        quote: AssetQuote = self.get_asset_quote(symbol)
        selected: Optional[float] = self.price_policy.select(quote, side=side)

        if selected is None or selected <= 0.0:
            raise ValueError(f"No usable execution price for symbol={symbol} side={side}")

        return float(selected)

    @staticmethod
    def _normalise_option_symbol(option_symbol: str) -> str:
        """
        Normalise an option symbol key for dictionary lookup.

        Note
        - Underlying symbols use the Symbol domain type and normalise_symbol().
        - Option symbols are broker-specific strings, so we normalise minimally.
        """
        return option_symbol.strip().upper()

    def get_option_quote(self, option_symbol: str) -> AssetQuote:
        """
        Retrieve the cached option quote for a broker-specific option symbol.

        Behaviour
        - Raises KeyError if missing.
        - Missing option quotes usually means the orchestrator did not fetch
          option quotes for the selected legs this cycle.

        Why this exists
        - Strategies and risk rules need leg pricing without broker IO.
        """
        key: str = self._normalise_option_symbol(option_symbol)

        if key not in self.option_quotes:
            raise KeyError(f"Missing option quote for option_symbol={key}")

        return self.option_quotes[key]

    def get_option_execution_price(self, option_symbol: str, side: PriceSide) -> float:
        """
        Retrieve an execution-aware price for an option contract and trade side.

        Behaviour
        - BUY uses ask (or conservative fallbacks) via PriceSelectionPolicy.
        - SELL uses bid (or conservative fallbacks) via PriceSelectionPolicy.

        Why this exists
        - PMCC sizing needs conservative pricing for both legs:
            - LEAP leg is typically a BUY (debit, prefer ask).
            - Near leg is typically a SELL (credit, prefer bid).
        """
        quote: AssetQuote = self.get_option_quote(option_symbol)
        selected: Optional[float] = self.price_policy.select(quote, side=side)

        if selected is None or selected <= 0.0:
            raise ValueError(f"No usable execution price for option_symbol={option_symbol} side={side}")

        return float(selected)
