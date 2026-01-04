# src/TradingBot/v2/orchestrator.py
#
# This module defines the V2 orchestrator for Option C.
#
# Option C rule
# - Only the orchestrator may talk to the broker (network IO and account IO).
# - Strategies and the risk engine must be pure with respect to broker IO.
#
# Why this exists
# - Ensures one coherent snapshot per cycle (RiskContext).
# - Prevents strategies bypassing risk controls.
# - Enables dry-run and unit testing with FakeBrokerV2.

from __future__ import annotations

# dataclass removes boilerplate for small classes that mostly store dependencies.
from dataclasses import dataclass

# datetime and timezone are used to create timezone-aware UTC timestamps.
from datetime import datetime, timezone

# Any describes broker payload fields we do not control.
# Dict and List describe container shapes.
# Set is used to deduplicate symbols so we only fetch each price once.
from typing import Any, Dict, List, Set, Optional

# BrokerInterfaceV2 is the only thing the orchestrator needs from the broker layer.
from TradingBot.v2.brokers.account_snapshot import AccountSnapshot
from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2

# RiskContext is the immutable snapshot passed to strategies and risk.
from TradingBot.v2.context import RiskContext

# TradeIntent is what strategies produce.
from TradingBot.v2.intents import TradeIntent

# RiskDecision is what the risk engine produces.
from TradingBot.v2.risk.decisions import RiskDecision

# RiskEngineV2 evaluates intents using rules.
from TradingBot.v2.risk.risk_engine import RiskEngineV2

# StrategyV2 is the contract for V2 strategies.
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2

# Symbol is a domain type (usually a str-like wrapper).
# normalise_symbol canonicalises user-input symbols into a stable Symbol.
from TradingBot.v2.domain.types import Symbol, normalise_symbol

# setup_logger provides consistent log formatting across V2 modules.
from TradingBot.v2.logger import setup_logger

# log_scope provides consistent "start/end + duration" logging across V2 modules.
from TradingBot.v2.logging_utils import log_scope


# Create a module-level logger once.
# The logger name appears in log output, so we do not prefix messages manually.
logger = setup_logger("Orchestrator")


@dataclass
class OrchestratorV2:
    """
    V2 orchestrator (Option C).

    Responsibilities
    - Perform all broker IO in one place.
    - Build exactly one RiskContext snapshot per cycle.
    - Ask strategies for TradeIntent objects (pure computation).
    - Ask risk engine for RiskDecision objects (pure computation).
    - Log outcomes.
    - Only submit orders when dry_run is False (submission not implemented yet).

    Notes on collections used here
    - List[T]: ordered, mutable sequence (useful for collecting items).
    - Dict[K, V]: mapping for fast lookup (symbol -> price).
    - Set[T]: unique collection (useful for deduplicating symbols).
    """

    # The broker adapter that provides account state and market data.
    broker: BrokerInterfaceV2

    # The strategies we run each cycle.
    strategies: List[StrategyV2]

    # The risk engine that evaluates intents.
    risk_engine: RiskEngineV2

    # Safety switch to prevent trading while architecture is still being validated.
    dry_run: bool = True

    def _now_utc(self) -> datetime:
        """
        Return a timezone-aware UTC timestamp.

        Why UTC
        - Avoids confusion across machines and deployments with different local time zones.
        - Makes logs comparable and consistent.
        """
        # datetime.now(timezone.utc) returns an aware datetime (has timezone info).
        now_utc: datetime = datetime.now(timezone.utc)
        return now_utc

    def _strategy_symbols(self) -> Set[Symbol]:
        """
        Collect the set of unique symbols required by all strategies.

        Why Set
        - Multiple strategies may request the same underlying.
        - A set removes duplicates automatically, so we only fetch each price once.

        Return value
        - A set of canonical Symbol values.
        """
        with log_scope("orchestrator._strategy_symbols", logger):
            # Start with an empty set.
            # Sets store unique values by definition.
            unique: Set[Symbol] = set()

            # Loop over each strategy instance.
            for strat in self.strategies:
                # strat.symbols is a strategy-provided sequence of required symbols.
                # It must be broker-free and side-effect free.
                for raw_symbol in strat.symbols:
                    # Convert to string defensively because raw_symbol may be Symbol or str.
                    raw_text: str = str(raw_symbol)

                    # Skip empty or whitespace-only values so we never normalise junk.
                    if not raw_text.strip():
                        continue

                    # Normalise into a canonical Symbol (trim + uppercase rules live in one place).
                    sym: Symbol = normalise_symbol(raw_text)

                    # Add the canonical symbol to the set.
                    unique.add(sym)

            logger.info("Strategy symbols collected count=%d symbols=%s", int(len(unique)), sorted([str(s) for s in unique]))
            return unique

    def _build_prices(self) -> Dict[Symbol, float]:
        """
        Fetch one price per unique underlying symbol.

        Why this exists
        - Price fetching is broker IO and must happen in the orchestrator.
        - Strategies must use RiskContext.get_price rather than broker calls.

        Return value
        - Dict[Symbol, float] mapping canonical symbols to positive float prices.
        - Missing or invalid prices are omitted.
        """
        with log_scope("orchestrator._build_prices", logger):
            # Initialise an empty mapping to fill.
            prices: Dict[Symbol, float] = {}

            # Deduplicate requested symbols first.
            # This avoids repeated broker calls for the same symbol.
            symbols: Set[Symbol] = self._strategy_symbols()

            # Fetch prices for each symbol.
            for sym in symbols:
                try:
                    with log_scope("broker.get_asset_price", logger, extra=f"symbol={sym}"):
                        # Broker call: fetch the current price for this symbol.
                        # BrokerInterfaceV2 expects a str symbol identifier.
                        raw_price: Any = self.broker.get_asset_price(str(sym))

                except KeyboardInterrupt:
                    # KeyboardInterrupt is a BaseException, not an Exception.
                    # If this fires unexpectedly, something external is interrupting the process.
                    # We log it loudly and re-raise so Ctrl+C still works as a true stop signal.
                    logger.exception("KeyboardInterrupt during get_asset_price for symbol=%s", sym)
                    raise

                except Exception as exc:
                    # Do not crash the whole cycle if one price fetch fails.
                    # We log a warning with context for diagnosis.
                    logger.exception("Failed to fetch price for %s: %s", sym, exc)
                    continue

                # Validate the returned price is numeric and positive.
                if isinstance(raw_price, (int, float)) and float(raw_price) > 0.0:
                    # Store as float for consistency across int and float inputs.
                    prices[sym] = float(raw_price)
                    logger.info("Price stored symbol=%s price=%.6f", sym, float(prices[sym]))
                else:
                    # Invalid price shapes are logged so broker adapters can be fixed.
                    logger.warning("Invalid price for %s: %r", sym, raw_price)

            logger.info("Prices built count=%d symbols=%s", int(len(prices)), sorted([str(s) for s in prices.keys()]))
            return prices

    def _build_context(self) -> RiskContext:
        """
        Build one immutable RiskContext snapshot for this cycle.

        What goes into the snapshot
        - Timestamp (UTC)
        - Option buying power and equity (floats)
        - Positions and open orders (broker payload snapshots)
        - Prices (Dict[Symbol, float]) for strategy evaluation
        """
        with log_scope("orchestrator._build_context", logger):
            # Capture the timestamp first so the snapshot has a clear "as-of" time.
            as_of_utc: datetime = self._now_utc()
            logger.info("Context timestamp as_of_utc=%s", as_of_utc.isoformat())

            # TO DO: optmisie this for efficienvty by parallelising broker calls. 
            # Currently, all calls are sequential which may slow down the cycle, but all the account info can be fetched in 
            # a single call within Alpaca. 
            # Broker IO: read account numeric values.
            # with log_scope("broker.get_option_buying_power", logger):
            #     option_buying_power_raw: Any = self.broker.get_option_buying_power()
            # with log_scope("broker.get_equity", logger):
            #     equity_raw: Any = self.broker.get_equity()
            with log_scope("broker.get_account_snapshot", logger):
                account_snapshot: AccountSnapshot = self.broker.get_account_snapshot()
                option_buying_power_raw: Any = account_snapshot.options_buying_power
                equity_raw: Any = account_snapshot.equity
            # Broker IO: read open orders and positions.
            with log_scope("broker.get_open_orders", logger):
                open_orders_raw: Any = self.broker.get_open_orders()
            with log_scope("broker.get_positions", logger):
                positions_raw: Any = self.broker.get_positions()

            # Convert buying power to float when possible, else fail safe to 0.0.
            option_buying_power: float = (
                float(option_buying_power_raw)
                if isinstance(option_buying_power_raw, (int, float))
                else 0.0
            )

            # Convert equity to float when possible, else fail safe to 0.0.
            equity: float = float(equity_raw) if isinstance(equity_raw, (int, float)) else 0.0

            # Enforce safe defaults if broker returns unexpected shapes.
            open_orders: List[Dict[str, Any]] = (
                open_orders_raw if isinstance(open_orders_raw, list) else []
            )
            positions: Dict[str, Any] = positions_raw if isinstance(positions_raw, dict) else {}

            logger.info(
                "Broker snapshot numeric | option_buying_power=%.2f equity=%.2f",
                float(option_buying_power),
                float(equity),
            )
            logger.info(
                "Broker snapshot collections | open_orders=%d positions=%d",
                int(len(open_orders)),
                int(len(positions)),
            )

            # Broker IO: fetch prices once for all strategies.
            prices: Dict[Symbol, float] = self._build_prices()

            # Build the immutable context object.
            ctx: RiskContext = RiskContext(
                as_of_utc=as_of_utc,
                option_buying_power=option_buying_power,
                equity=equity,
                positions=positions,
                open_orders=open_orders,
                prices=prices,
            )

            logger.info(
                "Context built | as_of_utc=%s option_buying_power=%.2f equity=%.2f open_orders=%d positions=%d prices=%d",
                ctx.as_of_utc.isoformat(),
                float(ctx.option_buying_power),
                float(ctx.equity),
                int(len(ctx.open_orders)),
                int(len(ctx.positions)),
                int(len(ctx.prices)),
            )

            return ctx

    def _collect_intents(self, ctx: RiskContext) -> List[TradeIntent]:
        """
        Ask each strategy to generate TradeIntent objects.

        Why this exists
        - Strategies propose trades (intents).
        - They must not size, approve, or submit.
        """
        with log_scope("orchestrator._collect_intents", logger):
            # Start with an empty list because we will extend it.
            all_intents: List[TradeIntent] = []

            # Loop over strategies in a deterministic order (the list order).
            for strat in self.strategies:
                try:
                    with log_scope("strategy.generate_intents", logger, extra=f"strategy_id={strat.strategy_id}"):
                        # Strategy computation: no broker IO should happen inside this call.
                        intents: List[TradeIntent] = strat.generate_intents(ctx)
                except Exception as exc:
                    # Keep the cycle alive even if one strategy fails.
                    # We log stack trace to make debugging possible.
                    logger.exception("Strategy %s failed to generate intents: %s", strat.strategy_id, exc)
                    continue

                logger.info("Strategy %s produced intents=%d", strat.strategy_id, int(len(intents)))

                # Extend the list with the returned intents.
                # extend adds each element of the list individually.
                all_intents.extend(intents)

            logger.info("Total intents collected=%d", int(len(all_intents)))
            return all_intents

    def _log_decisions(self, decisions: List[RiskDecision]) -> None:
        """
        Log risk decisions in a consistent way.

        Why this exists
        - Decisions are the visible output of the dry-run pipeline.
        - This creates an audit trail and makes debugging deterministic.
        """
        with log_scope("orchestrator._log_decisions", logger, extra=f"count={len(decisions)}"):
            for decision in decisions:
                if decision.rejected is not None:
                    logger.info(
                        "REJECTED intent_id=%s reason=%s",
                        decision.rejected.intent_id,
                        decision.rejected.reason,
                    )
                    continue

                if decision.approved is not None:
                    logger.info(
                        "APPROVED intent_id=%s client_order_id=%s",
                        decision.approved.intent_id,
                        decision.approved.client_order_id,
                    )
                    continue

                # If we get here, the decision object is malformed.
                logger.warning("RiskDecision invalid (no approved or rejected).")

    def run_cycle(self) -> None:
        """
        Execute one full Option C cycle.

        Flow
        - Build RiskContext snapshot (broker IO occurs here, once).
        - Ask strategies for intents (pure computation).
        - Evaluate intents via risk engine (pure computation).
        - Log decisions.
        - Stop if dry_run is True (no submission).
        """
        with log_scope("orchestrator.run_cycle", logger):
            # Build the snapshot first.
            ctx: RiskContext = self._build_context()

            # Log a one-line snapshot summary.
            logger.info(
                "Snapshot as_of_utc=%s option_buying_power=%s equity=%s open_orders=%s positions=%s prices=%s",
                ctx.as_of_utc.isoformat(),
                ctx.option_buying_power,
                ctx.equity,
                len(ctx.open_orders),
                len(ctx.positions),
                len(ctx.prices),
            )

            # Strategy pass: collect intents.
            intents: List[TradeIntent] = self._collect_intents(ctx)
            logger.info("Collected %s intents.", len(intents))

            # Risk pass: evaluate intents.
            with log_scope("risk_engine.evaluate", logger, extra=f"intents={len(intents)}"):
                decisions: List[RiskDecision] = self.risk_engine.evaluate(ctx, intents)
            logger.info("Risk engine produced %s decisions.", len(decisions))

            # Log each decision.
            self._log_decisions(decisions)

            # Hard safety stop for dry run.
            if self.dry_run:
                logger.info("Dry run enabled: no orders will be submitted.")
                return

            # Submission is intentionally not implemented yet.
            logger.warning("Dry run disabled, but submission is not implemented yet.")
