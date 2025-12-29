from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Sequence, Tuple

from TradingBot.v2.context import RiskContext
from TradingBot.v2.intents import PmccIntentPayload, SelectedOption, TradeIntent
from TradingBot.v2.strategy_interface_v2 import StrategyV2


@dataclass(frozen=True) #Frozen because strategy parameters should be immutable
class PmccStrategyV2(StrategyV2):
    """
    V2 PMCC strategy (Option C).

    What this class does
    - Reads a RiskContext snapshot (provided by the orchestrator).
    - Produces TradeIntent objects that describe a desired PMCC structure.

    What this class does NOT do
    - It does not size positions.
    - It does not submit orders.
    - It does not query the broker directly.
    - It does not apply risk policy.

    Why this skeleton exists
    - We want to validate the end-to-end V2 plumbing (orchestrator -> intents -> risk engine)
      before implementing real option-chain selection and sizing.
    """

    # Underlying symbol the PMCC strategy will target (for example "SPY").
    # This is used for price lookup in ctx.prices and for deduplication logic in risk rules.
    underlying_symbol: str

    # Stable identifier used by the orchestrator for logging and by the risk engine for per-strategy limits.
    # A stable id should not include timestamps or random values.
    _strategy_id: str = "pmcc_v2"

    @property
    def strategy_id(self) -> str:
        """
        Provide a stable identifier for this strategy instance.

        Why a property
        - The StrategyV2 interface requires it.
        - A property allows computed ids later if needed, while preserving the interface.
        """
        return self._strategy_id

    @property
    def symbols(self) -> Sequence[str]:
        """
        Symbols this strategy cares about.

        Why we return a sequence (not necessarily a list)
        - A sequence can be a list or a tuple.
        - Returning a tuple is a simple way to provide a fixed, immutable collection.

        How the orchestrator uses this
        - It can pre-fetch prices once per cycle and store them in RiskContext.prices.
        """
        return (self.underlying_symbol,)

    def generate_intents(self, ctx: RiskContext) -> List[TradeIntent]:
        """
        Generate TradeIntents for this cycle.

        Skeleton behaviour
        - If we cannot see a price for the underlying in the snapshot, produce no intents.
        - If we can see a price, produce exactly one placeholder intent.

        Why we return a list
        - A strategy might generate zero, one, or multiple intents.
        - A list is ordered and mutable, which is convenient for incremental building.
        """
        # Normalise the symbol so it matches how prices are keyed in RiskContext.
        symbol: str = self.underlying_symbol.strip().upper()

        # Read the underlying price from the snapshot.
        # The strategy does not fetch market data directly.
        price = ctx.get_price(symbol)
        if price is None:
            # Returning an empty list means "no proposals this cycle".
            return []

        # Create a cycle-specific identifier for this intent.
        # Why include time
        # - intent_id should be unique per cycle to keep logs and decisions traceable.
        # - This does not need to be cryptographically unique, only practically unique.
        as_of: str = ctx.as_of_utc.strftime("%Y%m%dT%H%M%SZ")
        intent_id: str = f"{self.strategy_id}:{symbol}:{as_of}"

        # Placeholder option selections.
        # Why placeholders
        # - We have not implemented option chain selection yet.
        # - We still need a valid payload shape to test the V2 pipeline.
        # - Using clearly fake symbols prevents accidental submission even if someone disables dry-run.
        #
        # These values will be replaced with real selections once we connect:
        # - option chain fetching (in orchestrator or a market-data layer)
        # - selection logic (still inside strategy)
        placeholder_leap: SelectedOption = SelectedOption(
            option_symbol=f"{symbol}_LEAP_PLACEHOLDER",
            ask_price=0.0,
            bid_price=0.0,
            delta=0.0,
            dte=0,
        )

        placeholder_near: SelectedOption = SelectedOption(
            option_symbol=f"{symbol}_NEAR_PLACEHOLDER",
            ask_price=0.0,
            bid_price=0.0,
            delta=0.0,
            dte=0,
        )

        payload: PmccIntentPayload = PmccIntentPayload(
            underlying_symbol=symbol,
            leap=placeholder_leap,
            near=placeholder_near,
        )

        # Tags are stored as a tuple.
        # Why a tuple
        # - Tuples are typically treated as immutable.
        # - This makes tags stable and prevents accidental modification later in the pipeline.
        tags: Tuple[str, ...] = ("v2", "pmcc", "placeholder")

        intent: TradeIntent = TradeIntent(
            intent_id=intent_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            payload=payload,
            time_in_force="day",
            tags=tags,
        )

        # Return a list of intents because the interface contract expects a list.
        return [intent]
