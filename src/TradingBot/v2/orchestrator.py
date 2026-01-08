# src/TradingBot/v2/orchestrator.py
#
# This file defines the V2 orchestrator for “Option C”.
#
# Option C is an architectural rule-set:
# - Only the orchestrator is allowed to do broker IO (network calls and account calls).
# - Strategies and the risk engine must be pure computation:
#   they read from RiskContext and return results, but they never call the broker.
#
# Why this file exists:
# - It creates exactly one coherent snapshot of the world per cycle (RiskContext).
# - It prevents strategies from bypassing risk controls.
# - It makes dry-run safe and predictable.
# - It enables unit tests by swapping the broker with FakeBrokerV2.
#
# Direction this file is heading (PMCC support):
# - Orchestrator fetches option chains and (later) option quotes as broker IO.
# - Strategies declare the shape they want (targets), but remain IO-free.
# - RiskContext becomes the single container for “all data needed this cycle”.

from __future__ import annotations

# dataclass is used to create small dependency-holding classes without boilerplate __init__ code.
from dataclasses import dataclass

# datetime and timezone are used to produce timezone-aware timestamps in UTC.
from datetime import datetime, timezone

# typing imports explain and constrain what shapes our data has.
# - Any: used when we receive dict fields from the broker that we do not control.
# - Dict: mapping types (key -> value).
# - List: ordered sequences.
# - Set: unique collections (useful for de-duplication).
# - Optional: a value that may be missing (None).
# - Callable: something that can be called like a function.
# - Tuple: fixed-length or immutable sequences (often used for tags).
from typing import Any, Dict, List, Set, Optional, Tuple

# AccountSnapshot is our internal type that represents the broker’s account state.
from TradingBot.v2.brokers.account_snapshot import AccountSnapshot

# BrokerInterfaceV2 is the abstract contract for broker adapters.
# The orchestrator only talks to this interface, never to a broker SDK directly.
from TradingBot.v2.brokers.broker_interface_v2 import BrokerInterfaceV2

# RiskContext is the immutable snapshot passed into strategies and the risk engine.
# Strategies and risk must read only from RiskContext and must not do broker IO.
from TradingBot.v2.context import RiskContext
# TradeIntent is the output produced by strategies.
# An intent describes “what the strategy wants to do”, not what gets executed.
from TradingBot.v2.intents import TradeIntent

# RiskDecision is the output produced by the risk engine.
# It represents whether an intent is approved or rejected and why.
from TradingBot.v2.risk.decisions import RiskDecision

# RiskEngineV2 evaluates intents produced by strategies.
# It applies rules (for example “do not place duplicate orders”).
from TradingBot.v2.risk.risk_engine import RiskEngineV2

# StrategyV2 is the base interface (contract) that all V2 strategies implement.
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2

# Domain types:
# - Symbol: canonical representation of a trading symbol.
# - normalise_symbol: converts user input into a stable canonical Symbol (trim, case, validation).
# - AssetQuote: bid/ask/mid quote structure used by strategies and risk.
# - OptionChainRequest: strategy-provided request describing which option chain data is needed.
from TradingBot.v2.domain.types import AssetQuote, Symbol, normalise_symbol, OptionChainRequest

# PriceSelectionPolicy lives under risk because choosing a price is part of decision logic.
# The orchestrator fetches raw quotes; the policy chooses which quote we treat as “execution price”.
from TradingBot.v2.risk.price_policy import DefaultPriceSelectionPolicy, PriceSelectionPolicy

# setup_logger creates a consistent logger for all modules in V2.
from TradingBot.v2.logger import setup_logger

# log_scope is a helper context manager that logs “start/end + elapsed time”.
from TradingBot.v2.logging_utils import log_scope


# Create a module-level logger.
# This logger is used by all functions in this file.
logger = setup_logger("Orchestrator")


@dataclass
class OrchestratorV2:
    """
    V2 orchestrator (Option C).

    This object owns the “cycle”.
    A cycle is one full run of:
    - gather broker state (IO)
    - build RiskContext snapshot
    - ask strategies for intents (no IO)
    - ask risk engine for decisions (no IO)
    - log results
    - optionally submit orders (not implemented yet)

    Responsibilities
    - Perform all broker IO in one place.
    - Build exactly one RiskContext snapshot per cycle.
    - Ask strategies for TradeIntent objects (pure computation).
    - Ask risk engine for RiskDecision objects (pure computation).
    - Log outcomes.
    - Only submit orders when dry_run is False (submission not implemented yet).
    """

    # broker is the adapter that knows how to talk to Alpaca (or a fake broker in tests).
    broker: BrokerInterfaceV2

    # strategies is the set of strategy instances run each cycle.
    strategies: List[StrategyV2]

    # risk_engine evaluates the intents produced by strategies.
    risk_engine: RiskEngineV2

    # dry_run protects the account.
    # When True, we build intents and decisions, but we do not submit orders.
    dry_run: bool = True

    def _now_utc(self) -> datetime:
        """
        Return the current time as a timezone-aware UTC datetime.

        Why UTC:
        - Makes logs consistent and comparable across machines.
        - Avoids bugs caused by local time zones and daylight saving.
        """
        now_utc: datetime = datetime.now(timezone.utc)
        return now_utc

    def _strategy_symbols(self) -> Set[Symbol]:
        """
        Collect unique underlying symbols required by all strategies.

        “Underlying symbols” are the stock/ETF symbols (for example SPY) that we fetch quotes for.

        Why Set:
        - A Set automatically removes duplicates.
        - If two strategies both require SPY, we only fetch SPY’s quote once.

        Return:
        - A set of canonical Symbol values.
        """
        with log_scope("orchestrator._strategy_symbols", logger):
            # unique holds the de-duplicated set of symbols.
            unique: Set[Symbol] = set()

            # Loop over every strategy.
            for strat in self.strategies:
                # Each strategy exposes “symbols” it needs for underlying quotes.
                for raw_symbol in strat.symbols:
                    # Convert anything into text, then clean it.
                    raw_text: str = str(raw_symbol)
                    if not raw_text.strip():
                        # Skip empty strings so we do not crash normalise_symbol.
                        continue

                    # normalise_symbol enforces a canonical, stable representation.
                    sym: Symbol = normalise_symbol(raw_text)
                    unique.add(sym)

            # Log what we collected for debugging and observability.
            logger.info(
                "Strategy symbols collected count=%d symbols=%s",
                int(len(unique)),
                sorted([str(s) for s in unique]),
            )
            return unique

    def _option_chain_symbols(self) -> Set[Symbol]:
        """
        Collect unique underlyings for which we must fetch option chains.

        Rules:
        - Only strategies that opt in (requires_option_chain=True) are considered.
        - Symbols are normalised so RiskContext keys are stable.

        Return:
        - A set of canonical Symbol values.
        """
        with log_scope("orchestrator._option_chain_symbols", logger):
            unique: Set[Symbol] = set()

            for strat in self.strategies:
                # Some strategies do not need option chains at all.
                # We check a flag on the strategy to avoid unnecessary broker IO.
                if not getattr(strat, "requires_option_chain", False):
                    continue

                # option_chain_symbols is the list of underlyings for which the strategy wants chains.
                for raw_symbol in strat.option_chain_symbols:
                    raw_text: str = str(raw_symbol)
                    if not raw_text.strip():
                        continue

                    sym: Symbol = normalise_symbol(raw_text)
                    unique.add(sym)

            logger.info(
                "Option-chain symbols collected count=%d symbols=%s",
                int(len(unique)),
                sorted([str(s) for s in unique]),
            )
            return unique
    
    def _build_option_chains(self) -> Dict[Tuple[Symbol, str], List[Dict[str, Any]]]:
        """
        Fetch option chains for the subset of underlyings that require them.

        This method does two separate jobs:
        - Fetch: call the broker and receive raw snapshot rows.
        - Decide: whether the result is acceptable to store in RiskContext.

        We also log a small preview of the chain so we can verify:
        - filters (expiry range etc) are being applied,
        - the broker returns bids/asks,
        - greeks are present (or not),
        before we apply freshness rules.
        """
        with log_scope("orchestrator._build_option_chains", logger):
            chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]] = {}

            requests: List[OptionChainRequest] = self._collect_option_chain_requests()
            logger.info("Option chain requests collected | count=%d", len(requests))

            for req in requests:
                try:
                    underlying: Symbol = normalise_symbol(req.underlying)
                    request_id: str = req.request_id
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "Invalid option chain request underlying; skipping | raw=%r error=%s",
                        req.underlying,
                        str(exc),
                    )
                    continue

                underlying_sym: str = str(underlying)

                try:
                    max_age_seconds: int = self._resolve_option_chain_max_age_seconds(feed=req.feed)

                    chain_raw_any: Any = self.broker.get_option_chain(
                        underlying_sym,
                        include_calls=req.include_calls,
                        include_puts=req.include_puts,
                        feed=req.feed,
                        max_age_seconds=max_age_seconds,
                        limit=req.limit,
                        strike_price_gte=req.strike_price_gte,
                        strike_price_lte=req.strike_price_lte,
                        expiration_date=req.expiration_date,
                        expiration_date_gte=req.expiration_date_gte,
                        expiration_date_lte=req.expiration_date_lte,
                        root_symbol=req.root_symbol,
                        updated_since=req.updated_since,
                    )
                    
                    logger.info(
                        "Option chain freshness policy | underlying=%s feed=%s max_age_seconds=%d",
                        underlying_sym,
                        str(req.feed),
                        int(max_age_seconds),
                    )

                except Exception as exc:
                    logger.exception(
                        "Failed to fetch option chain | underlying=%s error=%s",
                        underlying_sym,
                        str(exc),
                    )
                    continue

                if not isinstance(chain_raw_any, list) or not chain_raw_any:
                    logger.warning("Option chain empty or invalid | underlying=%s", underlying_sym)
                    continue

                chain_raw: List[Dict[str, Any]] = [
                    r for r in chain_raw_any if isinstance(r, dict)
                ]

                # Always log a preview for debugging, even if we later reject it as stale.
                self._log_option_chain_preview(
                    underlying_sym=underlying_sym,
                    chain_raw=chain_raw,
                    as_of_utc=self._now_utc(),
                    preview_rows=12,
                )

                # Policy A: broker stamps _is_stale on every row, so we can check any one.
                try:
                    sample: Dict[str, Any] = chain_raw[0]
                    is_stale: bool = bool(sample.get("_is_stale", False))
                    feed_used: str = str(sample.get("_feed", ""))
                    newest_ts: str = str(sample.get("_newest_ts", ""))
                except Exception:
                    is_stale = False
                    feed_used = ""
                    newest_ts = ""

                if is_stale:
                    logger.warning(
                        "Option chain marked stale by broker; not storing | underlying=%s feed=%s newest_ts=%s",
                        underlying_sym,
                        feed_used if feed_used else "<unknown>",
                        newest_ts if newest_ts else "<unknown>",
                    )
                    continue

                key: Tuple[Symbol, str] = (underlying, str(request_id))
                chains[key] = chain_raw
                logger.info(
                    "Option chain stored | underlying=%s contracts=%d",
                    underlying_sym,
                    int(len(chain_raw)),
                )

            logger.info("Option chains built | underlyings=%d", int(len(chains)))
            return chains

    def _collect_option_chain_requests(self) -> List[OptionChainRequest]:
        """
        Ask every strategy to provide its option chain requests.

        A strategy returns a list of OptionChainRequest objects describing:
        - which underlying symbol
        - whether calls/puts are wanted
        - which feed to use
        - freshness thresholds
        - optional limits

        Return:
        - One list containing requests from all strategies.
        """
        reqs: List[OptionChainRequest] = []

        for s in self.strategies:
            # Each strategy contributes 0 or more requests.
            reqs.extend(s.option_chain_requests())

        return reqs
    
    def _resolve_option_chain_max_age_seconds(self, *, feed: str) -> int: # the * indicates that all following parameters are keyword-only. Hence, feed must be passed as a keyword argument.
        # feed is the data feed type (for example "indicative" or "live").
        """
        Decide option-chain freshness thresholds centrally.

        Design intent
        - Strategies declare what data they need.
        - Orchestrator declares execution policy (freshness) based on run mode.

        Notes
        - Indicative feed may be delayed in paper mode.
        - During dry runs we allow older snapshots so development is not blocked.
        """
        feed_norm: str = str(feed).strip().lower()

        # Development mode: allow older snapshots so the pipeline continues to run.
        if bool(self.dry_run):
            return 259200  # 3 days

        # Paper mode: still allow some slack because indicative can lag.
        # If you have self.paper / broker mode accessible, use it here.
        # If not, keep it conservative.
        if feed_norm == "indicative":
            return 3600  # 1 hour

        # Live or high quality feeds: require genuinely fresh data.
        return 10

    def _log_option_chain_preview(
        self,
        *,  # the * indicates that all following parameters are keyword-only.
        underlying_sym: str,
        chain_raw: List[Dict[str, Any]],
        as_of_utc: datetime,
        preview_rows: int = 12,
    ) -> None:
        """
        Log a small, human-readable preview of an option chain snapshot.

        Purpose
        - Debug and verify that our API filters are working.
        - Make it obvious what the broker actually returned before we decide whether
        the data is fresh enough to use.

        What we log per row
        - DTE (days to expiry) computed from the OCC contract symbol.
        - Contract symbol.
        - Bid and ask.
        - Greeks when present.

        Notes
        - We compute DTE from the OCC symbol (last 15 chars contain YYMMDD + C/P + strike).
        - Alpaca snapshot payload fields can vary by entitlement/feed, so extraction is defensive.
        """
        if not chain_raw:
            logger.info("Option chain preview | underlying=%s empty=true", underlying_sym)
            return

        # RAW payload: log one representative row at INFO so it appears in your current output.
        # We keep it to the first row only to avoid dumping hundreds of lines.
        logger.info(
            "Option chain raw payload sample | underlying=%s sample=%r",
            underlying_sym,
            chain_raw[0],
        )

        logger.info(
            "Option chain preview begin | underlying=%s rows=%d showing=%d as_of_utc=%s",
            underlying_sym,
            int(len(chain_raw)),
            int(min(preview_rows, len(chain_raw))),
            as_of_utc.isoformat(),
        )

        shown: int = 0

        for row in chain_raw:
            if shown >= preview_rows:
                break

            contract_symbol_any: Any = row.get("contract_symbol")
            if not isinstance(contract_symbol_any, str) or not contract_symbol_any.strip():
                continue

            contract_symbol: str = contract_symbol_any.strip().upper()

            # Compute DTE from OCC symbol suffix: YYMMDD + (C/P) + strike(8 digits)
            dte: Optional[int] = None
            try:
                suffix: str = contract_symbol[-15:]
                yymmdd: str = suffix[0:6]
                expiry_utc: datetime = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=timezone.utc)
                dte = int((expiry_utc - as_of_utc).total_seconds() // 86400)
            except Exception:
                dte = None

            latest_quote: Any = row.get("latestQuote")
            if not isinstance(latest_quote, dict):
                latest_quote = {}

            bid_any: Any = latest_quote.get("bp")
            ask_any: Any = latest_quote.get("ap")

            bid: Optional[float]
            ask: Optional[float]
            try:
                bid = float(bid_any) if bid_any is not None else None
            except Exception:
                bid = None
            try:
                ask = float(ask_any) if ask_any is not None else None
            except Exception:
                ask = None

            # Greeks can be absent depending on feed/entitlements.
            greeks_any: Any = row.get("greeks")
            if not isinstance(greeks_any, dict):
                greeks_any = {}

            def _f(v: Any) -> Optional[float]:
                try:
                    return float(v)
                except Exception:
                    return None

            delta: Optional[float] = _f(greeks_any.get("delta"))
            gamma: Optional[float] = _f(greeks_any.get("gamma"))
            theta: Optional[float] = _f(greeks_any.get("theta"))
            vega: Optional[float] = _f(greeks_any.get("vega"))
            rho: Optional[float] = _f(greeks_any.get("rho"))
            iv: Optional[float] = _f(greeks_any.get("iv"))

            logger.info(
                "Chain row | underlying=%s dte=%s symbol=%s bid=%s ask=%s greeks(delta=%s gamma=%s theta=%s vega=%s rho=%s iv=%s)",
                underlying_sym,
                str(dte) if dte is not None else "<unknown>",
                contract_symbol,
                f"{bid:.6f}" if isinstance(bid, float) else "<na>",
                f"{ask:.6f}" if isinstance(ask, float) else "<na>",
                f"{delta:.6f}" if isinstance(delta, float) else "<na>",
                f"{gamma:.6f}" if isinstance(gamma, float) else "<na>",
                f"{theta:.6f}" if isinstance(theta, float) else "<na>",
                f"{vega:.6f}" if isinstance(vega, float) else "<na>",
                f"{rho:.6f}" if isinstance(rho, float) else "<na>",
                f"{iv:.6f}" if isinstance(iv, float) else "<na>",
            )

            shown += 1

        logger.info(
            "Option chain preview end | underlying=%s shown=%d",
            underlying_sym,
            int(shown),
        )


    def _build_asset_quotes(self) -> Dict[Symbol, AssetQuote]:
        """
        Fetch one quote per unique underlying symbol.

        Quotes are stored as a dictionary:
        - key: Symbol (for example Symbol("SPY"))
        - value: AssetQuote (bid/ask/mid)

        Why this exists:
        - Quote fetching is broker IO.
        - Only the orchestrator may do broker IO.
        - Strategies must read quotes from RiskContext only.

        Return:
        - Dict[Symbol, AssetQuote]
        """
        with log_scope("orchestrator._build_asset_quotes", logger):
            quotes: Dict[Symbol, AssetQuote] = {}
            symbols: Set[Symbol] = self._strategy_symbols()

            for sym in symbols:
                try:
                    # log_scope records timing and makes it easy to locate slow calls.
                    with log_scope("broker.get_asset_quote", logger, extra=f"symbol={sym}"):
                        quote: AssetQuote = self.broker.get_asset_quote(str(sym))
                except KeyboardInterrupt:
                    # If the user stops the program, re-raise immediately.
                    logger.exception("KeyboardInterrupt during get_asset_quote for symbol=%s", sym)
                    raise
                except Exception as exc:
                    # Network errors or broker issues should not crash the full cycle.
                    logger.exception("Failed to fetch quote for %s: %s", sym, exc)
                    continue

                # Store the quote so strategies can read it from RiskContext.
                quotes[sym] = quote

                # Log the quote for observability.
                logger.info(
                    "Quote stored symbol=%s bid=%s ask=%s mid=%s",
                    sym,
                    quote.bid,
                    quote.ask,
                    quote.mid,
                )

            logger.info(
                "Asset quotes built count=%d symbols=%s",
                int(len(quotes)),
                sorted([str(s) for s in quotes.keys()]),
            )
            return quotes
        
    def _build_context(self) -> RiskContext:
        """
        Build one immutable RiskContext snapshot for this cycle.

        RiskContext is the one object that strategies and risk are allowed to read.

        What goes into the snapshot:
        - as_of_utc: timestamp for “this cycle's view of the world”
        - option_buying_power: broker-reported options buying power
        - equity: broker-reported account equity
        - positions: current broker positions snapshot
        - open_orders: current broker open orders snapshot
        - asset_quotes: underlying quotes fetched by orchestrator
        - price_policy: policy used to choose execution-aware prices
        - option_chains: option chain snapshots fetched by orchestrator

        PMCC support:
        - Option chains are fetched only when a strategy opts in via requires_option_chain.
        - Option chains are injected into RiskContext.option_chains.
        """
        with log_scope("orchestrator._build_context", logger):
            # Timestamp for this cycle.
            as_of_utc: datetime = self._now_utc()
            logger.info("Context timestamp as_of_utc=%s", as_of_utc.isoformat())

            # Broker IO: fetch account snapshot.
            with log_scope("broker.get_account_snapshot", logger):
                account_snapshot: AccountSnapshot = self.broker.get_account_snapshot()
                option_buying_power_raw: Any = account_snapshot.options_buying_power
                equity_raw: Any = account_snapshot.equity

            # Broker IO: fetch open orders and positions.
            with log_scope("broker.get_open_orders", logger):
                open_orders_raw: Any = self.broker.get_open_orders()
            with log_scope("broker.get_positions", logger):
                positions_raw: Any = self.broker.get_positions()

            # Convert broker numeric values into floats defensively.
            option_buying_power: float = (
                float(option_buying_power_raw)
                if isinstance(option_buying_power_raw, (int, float))
                else 0.0
            )
            equity: float = float(equity_raw) if isinstance(equity_raw, (int, float)) else 0.0

            # Convert broker collections into expected shapes defensively.
            open_orders: List[Dict[str, Any]] = open_orders_raw if isinstance(open_orders_raw, list) else []
            positions: List[Dict[str, Any]] = positions_raw if isinstance(positions_raw, list) else []

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

            # Broker IO: fetch underlying asset quotes.
            asset_quotes: Dict[Symbol, AssetQuote] = self._build_asset_quotes()

            # Broker IO: fetch option chain snapshots only if at least one strategy opted in.
            # any() checks if any strategy has requires_option_chain=True. getattr() is used for safety, 
            # to return False if the attribute does not exist.
            needs_option_chains: bool = any(
                bool(getattr(s, "requires_option_chain", False)) for s in self.strategies
            )

            option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]]
            if needs_option_chains:
                option_chains = self._build_option_chains()
            else:
                option_chains = {}
                logger.info("Skipping option chain fetch | reason=no_strategy_requires_option_chain")


            # Policy object used by RiskContext when selecting execution prices.
            price_policy: PriceSelectionPolicy = DefaultPriceSelectionPolicy()

            # Create the immutable RiskContext snapshot.
            ctx: RiskContext = RiskContext(
                as_of_utc=as_of_utc,
                option_buying_power=option_buying_power,
                equity=equity,
                positions=positions,
                open_orders=open_orders,
                asset_quotes=asset_quotes,
                price_policy=price_policy,
                option_chains=option_chains,
            )

            logger.info(
                "Context built | as_of_utc=%s option_buying_power=%.2f equity=%.2f open_orders=%d positions=%d asset_quotes=%d option_chains=%d",
                ctx.as_of_utc.isoformat(),
                float(ctx.option_buying_power),
                float(ctx.equity),
                int(len(ctx.open_orders)),
                int(len(ctx.positions)),
                int(len(ctx.asset_quotes)),
                int(len(ctx.option_chains)),
            )

            return ctx

    def _collect_intents(self, ctx: RiskContext) -> List[TradeIntent]:
        """
        Ask each strategy to generate TradeIntent objects using the current RiskContext.

        Why this exists:
        - Orchestrator owns IO and context creation.
        - Strategies are pure: they read ctx, then return intents.

        Return:
        - A combined list of intents produced by all strategies.
        """
        with log_scope("orchestrator._collect_intents", logger):
            all_intents: List[TradeIntent] = []

            for strat in self.strategies:
                try:
                    # Generate intents for this strategy.
                    with log_scope(
                        "strategy.generate_intents",
                        logger,
                        extra=f"strategy_id={strat.strategy_id}",
                    ):
                        intents: List[TradeIntent] = strat.generate_intents(ctx)
                except Exception as exc:
                    # Strategy errors should not crash the full orchestrator cycle.
                    logger.exception(
                        "Strategy %s failed to generate intents: %s",
                        strat.strategy_id,
                        exc,
                    )
                    continue

                logger.info("Strategy %s produced intents=%d", strat.strategy_id, int(len(intents)))
                all_intents.extend(intents)

            logger.info("Total intents collected=%d", int(len(all_intents)))
            return all_intents

    def _log_decisions(self, decisions: List[RiskDecision]) -> None:
        """
        Log risk decisions in a consistent way.

        Why this exists:
        - In dry-run mode, decisions are the main visible output.
        - Logging creates an audit trail for debugging.
        """
        with log_scope("orchestrator._log_decisions", logger, extra=f"count={len(decisions)}"):
            for decision in decisions:
                # A decision contains either an approved or rejected record.
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

                # If neither approved nor rejected is present, the object is malformed.
                logger.warning("RiskDecision invalid (no approved or rejected).")

    def run_cycle(self) -> None:
        """
        Execute one full orchestrator cycle.

        Flow:
        - Build RiskContext snapshot (broker IO occurs here).
        - Ask strategies for intents (pure computation).
        - Evaluate intents via risk engine (pure computation).
        - Log decisions.
        - If dry_run is True, stop without submission.
        """
        with log_scope("orchestrator.run_cycle", logger):
            # Build the snapshot that strategies and risk are allowed to read.
            ctx: RiskContext = self._build_context()

            # Log key snapshot features for debugging.
            logger.info(
                "Snapshot as_of_utc=%s option_buying_power=%s equity=%s open_orders=%s positions=%s asset_quotes=%s",
                ctx.as_of_utc.isoformat(),
                ctx.option_buying_power,
                ctx.equity,
                len(ctx.open_orders),
                len(ctx.positions),
                len(ctx.asset_quotes),
            )

            # Strategies propose intents.
            intents: List[TradeIntent] = self._collect_intents(ctx)
            logger.info("Collected %s intents.", len(intents))

            # Risk engine approves or rejects intents.
            with log_scope("risk_engine.evaluate", logger, extra=f"intents={len(intents)}"):
                decisions: List[RiskDecision] = self.risk_engine.evaluate(ctx, intents)
            logger.info("Risk engine produced %s decisions.", len(decisions))

            # Log decisions for observability.
            self._log_decisions(decisions)

            # If dry run is enabled, stop here.
            if self.dry_run:
                logger.info("Dry run enabled: no orders will be submitted.")
                return

            # Submission is not implemented yet.
            logger.warning("Dry run disabled, but submission is not implemented yet.")
