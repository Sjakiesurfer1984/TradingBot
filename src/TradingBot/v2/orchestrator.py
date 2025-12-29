# src/TradingBot/v2/orchestrator.py
#
# This module defines the V2 orchestrator for the Option C architecture.
#
# Why this file exists
# - In Option C, only one layer is allowed to talk to the broker (network and account IO).
# - That layer is the orchestrator.
# - Strategies and the risk engine must be pure with respect to broker IO, meaning they do not
#   call the broker directly. They only consume data passed in.
#
# Why this matters
# - It prevents strategies from accidentally bypassing risk controls.
# - It guarantees that a "cycle" uses a consistent snapshot of account state.
# - It makes behaviour reproducible and testable, because we can build a RiskContext in tests
#   without requiring a live broker connection.

from __future__ import annotations

# dataclass is used to reduce boilerplate in classes that mainly store data.
# It automatically generates an __init__ method and other helpful methods.
from dataclasses import dataclass

# datetime and timezone are used to stamp snapshots with an explicit UTC time.
# A timezone aware timestamp avoids ambiguity and makes logs easier to correlate.
from datetime import datetime, timezone

# Any, Dict, List are typing tools that describe the shapes of values.
# These do not change runtime behaviour, but they make intentions explicit and help static checking.
# - Any means "unknown type" or "broker specific shape".
# - Dict[K, V] means a mapping from keys of type K to values of type V.
# - List[T] means an ordered, mutable collection of items of type T.
from typing import Any, Dict, List

# The broker interface defines the operations the orchestrator is allowed to perform.
# Importing the interface rather than a concrete broker keeps the orchestrator decoupled.
from TradingBot.brokers.broker_interface import BrokerInterface

# The project logger factory provides consistent formatting and naming.
from TradingBot.logger import setup_logger

# RiskContext is the immutable snapshot built once per cycle and passed to strategies and risk.
from TradingBot.v2.context import RiskContext

# RiskDecision is the output of the risk engine, containing an approval or rejection.
from TradingBot.v2.decisions import RiskDecision

# RiskEngineV2 contains the risk policy logic for V2.
from TradingBot.v2.risk_engine import RiskEngineV2

# StrategyV2 defines the V2 strategy contract, which produces intents from a context.
from TradingBot.v2.strategy_interface_v2 import StrategyV2


# The logger name helps you filter logs when multiple components write messages.
logger = setup_logger("OrchestratorV2")


@dataclass
class OrchestratorV2:
    """
    V2 orchestrator (Option C).

    Design intent
    - This class is the only component allowed to talk to the broker.
    - It builds a consistent RiskContext snapshot once per cycle.
    - It asks strategies for TradeIntents.
    - It asks the risk engine for RiskDecisions.
    - It logs the outcomes in a consistent format.
    - It does not submit orders while dry_run is True.

    A note on collections (List, Dict, Tuple)
    - A List is an ordered collection that can be changed (items can be appended or removed).
      We use lists when we are building up a collection during a cycle.
    - A Dict is a mapping from keys to values. We use dicts when we want quick lookup by key,
      such as mapping symbol -> price.
    - A Tuple is an ordered collection that is typically treated as fixed size and immutable.
      In this file we do not create tuples directly, but you will see tuples used elsewhere
      (such as intent tags) when we want a stable, non-mutable set of values.

    A note on comprehensions
    - Python supports list comprehensions and dict comprehensions. They are compact ways of
      creating a new list or dict from existing data.
    - In this file we prefer explicit loops for clarity, because this orchestrator is a central
      control layer and readability matters more than compactness.
    """

    # The broker is the single gateway to account state and market data.
    # This is why it belongs in the orchestrator rather than in strategies.
    broker: BrokerInterface

    # Strategies are stored in a list because:
    # - We have a sequence of strategies to run each cycle.
    # - The order can matter for logging and deterministic behaviour.
    # - We may append or remove strategies as the system grows.
    strategies: List[StrategyV2]

    # The risk engine is the component that applies risk policy to intents.
    # It produces decisions, which the orchestrator can later execute.
    risk_engine: RiskEngineV2

    # dry_run is a safety switch.
    # When True, the orchestrator will not submit orders to the broker.
    # This is essential while we are still building sizing and approval logic.
    dry_run: bool = True

    def _now_utc(self) -> datetime:
        """
        Return the current time as a timezone aware UTC datetime.

        Why this exists
        - We want every snapshot to be labelled with a precise time.
        - Using UTC avoids confusion when running in different timezones.
        - Using an aware datetime (with timezone) avoids ambiguous timestamps.
        """
        # datetime.now(timezone.utc) returns an aware datetime with UTC timezone info.
        now_utc: datetime = datetime.now(timezone.utc)
        return now_utc

    def _build_prices(self) -> Dict[str, float]:
        """
        Fetch one underlying price per strategy symbol for this cycle.

        Why this exists
        - Market data fetching is broker IO, so it must live here.
        - Strategies should not call the broker, they should consume ctx.prices instead.
        - A dict allows fast lookup by symbol when strategies need the price.

        Return value
        - A dict mapping uppercase symbols to a positive float price.
        - Symbols with missing or invalid prices are omitted.
        """
        # Initialise an empty dict. It will be filled during the loop.
        prices: Dict[str, float] = {}

        # Loop over each strategy in the list.
        # A for loop is explicit and easy to debug.
        for strat in self.strategies:
            # Strategies may store the symbol on an attribute called "symbol".
            # getattr provides safe access with a default of None if absent.
            raw_symbol: Any = getattr(strat, "symbol", None)

            # We require a non-empty string symbol to fetch a price.
            if not isinstance(raw_symbol, str):
                continue
            if not raw_symbol.strip():
                continue

            # Normalise the symbol.
            # strip removes surrounding whitespace.
            # upper makes it uppercase for consistent keys.
            sym: str = raw_symbol.strip().upper()

            try:
                # This is broker IO.
                # The broker should return a numeric price or raise an exception.
                raw_price: Any = self.broker.get_asset_price(sym)
            except Exception as exc:
                # If a broker call fails, we log and skip the symbol.
                # We do not raise here because one missing price should not crash the cycle.
                logger.warning(f"Failed to fetch price for {sym}: {exc}")
                continue

            # Validate that the broker returned a number and that it is positive.
            if isinstance(raw_price, (int, float)) and float(raw_price) > 0.0:
                # Store as float to normalise int and float values consistently.
                prices[sym] = float(raw_price)
            else:
                # Log invalid price shape to help diagnose broker adapter issues.
                logger.warning(f"Broker returned invalid price for {sym}: {raw_price}")

        return prices

    def _build_context(self) -> RiskContext:
        """
        Build a single immutable RiskContext snapshot for this cycle.

        Why this exists
        - Option C requires one coherent snapshot for all strategy evaluation.
        - If we queried the broker separately inside each strategy, the data could drift
          within the same cycle, leading to inconsistent risk decisions.

        What is included
        - Timestamp for the snapshot.
        - Option buying power and equity for risk policy.
        - Open orders and positions for deduplication and exposure checks.
        - Underlying prices for strategies and later sizing.
        """
        # Record the time at which the snapshot is taken.
        as_of_utc: datetime = self._now_utc()

        # Fetch account level values from the broker.
        # These methods should be implemented by the broker adapter.
        option_buying_power_raw: Any = self.broker.get_option_buying_power()
        equity_raw: Any = self.broker.get_equity()

        # Fetch open orders and positions.
        open_orders_raw: Any = self.broker.get_open_orders()
        positions_raw: Any = self.broker.get_positions()

        # Convert buying power to float if possible.
        # If it is missing or invalid, fall back to 0.0 so the system fails safe.
        option_buying_power: float = (
            float(option_buying_power_raw)
            if isinstance(option_buying_power_raw, (int, float))
            else 0.0
        )

        # Convert equity to float if possible.
        # This supports later rules such as max daily loss or drawdown limits.
        equity: float = float(equity_raw) if isinstance(equity_raw, (int, float)) else 0.0

        # Open orders are expected to be a list of dicts, but broker payloads can vary.
        # If invalid, we use an empty list.
        # An empty list means "no open orders known", which is safe when combined with
        # conservative risk rules.
        open_orders: List[Dict[str, Any]] = (
            open_orders_raw if isinstance(open_orders_raw, list) else []
        )

        # Positions are expected to be a dict keyed by symbol.
        # If invalid, we use an empty dict.
        positions: Dict[str, Any] = positions_raw if isinstance(positions_raw, dict) else {}

        # Build the price mapping once per cycle.
        prices: Dict[str, float] = self._build_prices()

        # Construct the immutable RiskContext dataclass.
        # This object is passed to strategies and risk rules.
        ctx: RiskContext = RiskContext(
            as_of_utc=as_of_utc,
            option_buying_power=option_buying_power,
            equity=equity,
            positions=positions,
            open_orders=open_orders,
            prices=prices,
        )

        return ctx

    def _collect_intents(self, ctx: RiskContext) -> List[Any]:
        """
        Ask all strategies for intents using the same RiskContext.

        Why this exists
        - Strategies in Option C are intent only.
        - They must not place orders or size positions.
        - They should be able to be run in any order without side effects.

        Return value
        - A list of intents produced by all strategies.
        - The element type is Any here to avoid coupling this orchestrator to a specific
          intent class during early iterations. As V2 stabilises, you can replace Any
          with TradeIntent explicitly.
        """
        # Initialise an empty list because we will append items.
        # A list is appropriate because order is useful for debugging and replay.
        all_intents: List[Any] = []

        # Iterate over each strategy.
        for strat in self.strategies:
            try:
                # generate_intents is a strategy interface method.
                # It consumes RiskContext and returns either a list of intents or a single intent.
                intents: Any = strat.generate_intents(ctx)
            except Exception as exc:
                # A strategy failure should not crash the entire bot.
                # We log the exception with stack trace for diagnosis.
                strategy_id: Any = getattr(strat, "strategy_id", "UNKNOWN")
                logger.exception(f"Strategy {strategy_id} failed: {exc}")
                continue

            # If the strategy returns None or an empty collection, there are no intents.
            if not intents:
                continue

            # If the strategy returns a list, extend our list.
            # extend adds each element individually.
            if isinstance(intents, list):
                all_intents.extend(intents)
            else:
                # If the strategy returns a single intent, append it as one item.
                all_intents.append(intents)

        return all_intents

    def _log_decisions(self, decisions: List[RiskDecision]) -> None:
        """
        Log risk decisions in a consistent, human-readable format.

        Why this exists
        - In dry run mode, logs are the primary output.
        - This logging becomes an audit trail and debugging tool.
        - A consistent format enables grepping and later log parsing.
        """
        # Iterate over each decision.
        for decision in decisions:
            # A rejected decision contains a RejectedIntent instance.
            if decision.rejected is not None:
                intent_id: str = decision.rejected.intent_id
                reason: str = decision.rejected.reason
                logger.info(f"REJECTED intent_id={intent_id} reason={reason}")
                continue

            # An approved decision contains an ApprovedOrder instance.
            if decision.approved is not None:
                intent_id = decision.approved.intent_id
                client_order_id = decision.approved.client_order_id
                logger.info(f"APPROVED intent_id={intent_id} client_order_id={client_order_id}")
                continue

            # If neither is present, the decision object violates its invariant.
            # This indicates a programming error in the risk engine.
            logger.warning("RiskDecision has neither approved nor rejected populated (invalid state).")

    def run_cycle(self) -> None:
        """
        Execute one full Option C orchestration cycle.

        Cycle stages
        - Build RiskContext snapshot
        - Collect intents from strategies
        - Evaluate risk decisions
        - Log results
        - Do not submit orders while dry_run is True

        Why this should not submit orders yet
        - We are still building the risk engine rules and sizing logic.
        - Any premature submission would be unsafe and difficult to diagnose.
        """
        # Build a single snapshot for this cycle.
        ctx: RiskContext = self._build_context()

        # Log a summary of the snapshot.
        # len(open_orders) counts the number of open order records.
        # len(positions) counts the number of keys in the positions dict.
        logger.info(
            f"Cycle snapshot as_of_utc={ctx.as_of_utc.isoformat()} "
            f"option_buying_power={ctx.option_buying_power} "
            f"equity={ctx.equity} "
            f"open_orders={len(ctx.open_orders)} "
            f"positions={len(ctx.positions)}"
        )

        # Ask strategies to produce intents from this snapshot.
        intents: List[Any] = self._collect_intents(ctx)
        logger.info(f"Collected {len(intents)} intents.")

        # Ask the risk engine to evaluate the intents.
        # The risk engine must use ctx, not broker IO.
        decisions: List[RiskDecision] = self.risk_engine.evaluate(ctx, intents)
        logger.info(f"Risk engine produced {len(decisions)} decisions.")

        # Log each decision outcome.
        self._log_decisions(decisions)

        # If dry_run is enabled, we stop here.
        # This ensures there are no broker write actions.
        if self.dry_run:
            logger.info("Dry run enabled: no orders will be submitted.")
            return

        # If dry_run is disabled, we would submit approved orders here.
        # Submission is intentionally not implemented yet.
        # When we implement submission, it must be done in this orchestrator only.
        logger.warning("Dry run disabled, but order submission is not implemented yet.")
