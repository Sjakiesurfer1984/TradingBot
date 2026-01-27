# src/TradingBot/v2/strategy_interface_v2.py
#
# This module defines the V2 strategy interface used by the Option C architecture.
#
# Why this file exists
# - In V1, strategies often held a broker reference and could fetch account state and place orders.
# - In V2 Option C, strategies must be intent-only: they propose trades but never execute them.
# - This interface is the contract that prevents V2 from drifting back into V1-style coupling.
#
# Key idea
# - Strategy consumes a RiskContext snapshot (built by the orchestrator).
# - Strategy produces TradeIntent objects (requests).
# - Strategy never sizes positions and never submits orders.

from __future__ import annotations

# ABC and abstractmethod are used to define an interface (an abstract base class).
# An abstract base class cannot be instantiated directly.
# It forces subclasses to implement specific methods and properties.
from abc import ABC, abstractmethod

# dataclass is not used directly in this file, but strategies often are dataclasses.
# We avoid importing it here to keep the interface minimal and focused.
from typing import List, Sequence

# RiskContext is the only input into V2 strategies.
# It is an immutable snapshot, which keeps the strategy deterministic.
from TradingBot.v2.context import RiskContext

# TradeIntent is the output contract from V2 strategies.
# Intents describe what the strategy wants to do, not what it is allowed to do.
from TradingBot.v2.intents import TradeIntent

# StrategyId and Symbol are core domain identifiers.
# OptionChainRequest expresses what the orchestrator should fetch for option chain snapshots.
from TradingBot.v2.domain.types import StrategyId, Symbol, OptionChainRequest, normalise_symbol

from TradingBot.v2.logger import setup_logger

logger = setup_logger("Strategy Interface")


class Strategy(ABC):
    """
    Interface for V2 strategies (Option C).

    Design intent
    - A Strategy analyses a RiskContext snapshot and proposes one or more TradeIntents.
    - It does not execute trades.
    - It does not decide risk.
    - It does not allocate capital.
    - It does not talk to the broker.

    Why this matters
    - It keeps risk centralised and auditable.
    - It makes strategies testable: you can unit-test them with a fake RiskContext.
    - It prevents hidden broker IO inside strategy code.

    Terms used here
    - "intent" means a proposed action that has not been approved or sized.
    - "cycle" means one orchestrator tick, where a snapshot is built and strategies run once.

    Collections notes
    - We return a list (or sequence) of intents because:
      - a strategy may propose zero, one, or many intents in a cycle
      - a list preserves order, which aids debugging and reproducibility
    """

    @property
    @abstractmethod
    def strategy_id(self) -> StrategyId:
        """
        Return a stable identifier for this strategy.

        Why this exists
        - Risk policies often need to apply limits per strategy.
        - Logging and audit trails require stable names.
        - Orchestrator may group decisions and metrics by strategy_id.

        Requirements
        - Must be a non-empty string.
        - Should be stable across runs (do not generate randomly).
        """
        raise NotImplementedError

    @property
    def symbols(self) -> Sequence[Symbol]:
        """
        Return the primary symbols this strategy cares about.

        Why this exists
        - The orchestrator may pre-fetch prices for these symbols once per cycle.
        - Centralising symbol discovery here avoids the orchestrator inspecting strategy internals.

        Default behaviour
        - Returns an empty sequence, meaning the strategy does not request pre-fetched prices.

        Notes
        - A "sequence" is a general interface that includes list and tuple.
          We use Sequence rather than List to allow strategies to return tuples for immutability.
        """
        # IMPORTANT
        # - The interface must not assume any strategy-specific attributes exist (eg underlying_symbol).
        # - Concrete strategies must override this property and return their required symbols.
        return ()

    @property
    def requires_option_chain(self) -> bool:
        """
        Return True if this strategy requires option-chain data.

        Why this exists
        - Keeps broker IO centralised in the orchestrator.
        - Allows orchestrator to fetch option chains only when needed.

        Default
        - False for most strategies.
        """
        return False

    @property
    def option_chain_symbols(self) -> Sequence[Symbol]:  # Sequence is a more general type than List. The difference is that Sequence includes tuples and other ordered collections, not just lists.
        """
        Declare the set of underlyings for which the orchestrator should fetch option chains.

        Default behaviour
        - Uses the same symbols declared by symbols.

        Strategies with special needs may override this property.
        """
        # NOTE
        # - This must return actual symbols, not a property or a method object.
        # - Because symbols is a @property, self.symbols evaluates to a Sequence[Symbol].
        return self.symbols

    @abstractmethod
    def generate_intents(self, ctx: RiskContext) -> List[TradeIntent]:
        """
        Generate trade intents based on the current RiskContext snapshot.

        Parameters
        - ctx:
            Immutable snapshot of account and market state for this cycle.

        Returns
        - List[TradeIntent]:
            A list of intents to be evaluated by the risk engine.
            Returning an empty list means "no trade proposals this cycle".

        Rules (must follow)
        - Must not call broker methods directly.
        - Must not size trades.
        - Must not create broker order objects.
        - Must not submit orders.
        - Must not mutate ctx.

        Error handling
        - It is acceptable to raise exceptions if the strategy cannot proceed.
          The orchestrator is responsible for catching and logging strategy failures.
        """
        raise NotImplementedError

    def option_chain_requests(self) -> List[OptionChainRequest]:
        # Default behaviour
        # - No option chain requests from the base interface.
        # - Concrete strategies can override this to request specific expiries, types, etc.
        return []
