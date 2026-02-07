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

from __future__ import annotations  # postponed evaluation of type annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from TradingBot.brokers.account_snapshot import AccountSnapshot
from TradingBot.brokers.broker_interface import BrokerInterface

from TradingBot.orchestration.context import RiskContext
from TradingBot.domain.types import AssetQuote, OptionChainRequest, Symbol, normalise_symbol
from TradingBot.domain.intents import TradeIntent

from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope
from TradingBot.risk.decisions import RiskDecision
from TradingBot.risk.price_policy import DefaultPriceSelectionPolicy, PriceSelectionPolicy
from TradingBot.risk.risk_engine import RiskEngine
from TradingBot.strategies.strategy_interface import Strategy

from TradingBot.orchestration.cycle_plan import CyclePlan, build_cycle_plan


logger = setup_logger("Orchestrator")


@dataclass(frozen=True)
class _BaseSnapshot:
    """
    Minimal broker snapshot used to decide which expensive IO to perform next.

    This is intentionally smaller than RiskContext. It exists so we can:
    - fetch open_orders and positions first
    - build a CyclePlan
    - then decide whether to fetch option chains
    """

    as_of_utc: datetime
    option_buying_power: float
    equity: float
    open_orders: List[Dict[str, Any]]
    positions: List[Dict[str, Any]]


@dataclass
class Orchestrator:
    """
    orchestrator (Option C).

    This object owns the “cycle”.
    A cycle is one full run of:
    - gather broker state (IO)
    - build RiskContext snapshot
    - ask strategies for intents (no IO)
    - ask risk engine for decisions (no IO)
    - log results
    - optionally submit orders
    """

    broker: BrokerInterface
    strategies: List[Strategy]
    risk_engine: RiskEngine
    dry_run: bool = True

    def _now_utc(self) -> datetime:
        now_utc: datetime = datetime.now(timezone.utc)
        return now_utc

    def _strategy_symbols(self) -> Set[Symbol]:
        """
        Collect unique underlying symbols required by all strategies.
        """
        with log_scope("orchestrator._strategy_symbols", logger):
            unique: Set[Symbol] = set()

            for strat in self.strategies:
                for raw_symbol in strat.symbols:
                    raw_text: str = str(raw_symbol)
                    if not raw_text.strip():
                        continue

                    sym: Symbol = normalise_symbol(raw_text)
                    unique.add(sym)

            logger.info(
                "Strategy symbols collected count=%d symbols=%s",
                int(len(unique)),
                sorted([str(s) for s in unique]),
            )
            return unique

    def _collect_option_chain_requests(self) -> List[OptionChainRequest]:
        """
        Ask every strategy to provide its option chain requests.
        """
        reqs: List[OptionChainRequest] = []
        for s in self.strategies:
            reqs.extend(s.option_chain_requests())
        return reqs

    def _resolve_option_chain_max_age_seconds(self, *, feed: str) -> int:
        feed_norm: str = str(feed).strip().lower()

        if bool(self.dry_run):
            return 259200  # 3 days

        if feed_norm == "indicative":
            return 259200  # 3 days

        return 10

    def _log_option_chain_preview(
        self,
        *,
        underlying_sym: str,
        chain_raw: List[Dict[str, Any]],
        as_of_utc: datetime,
        preview_rows: int = 12,
    ) -> None:
        if not chain_raw:
            logger.info("Option chain preview | underlying=%s empty=true", underlying_sym)
            return

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

        logger.info("Option chain preview end | underlying=%s shown=%d", underlying_sym, int(shown))

    def _build_base_snapshot(self) -> _BaseSnapshot:
        """
        Broker IO (cheap-ish): account + open orders + positions.
        This happens before we decide whether to do expensive IO (option chains).
        """
        with log_scope("orchestrator._build_base_snapshot", logger):
            as_of_utc: datetime = self._now_utc()
            logger.info("Context timestamp as_of_utc=%s", as_of_utc.isoformat())

            with log_scope("broker.get_account_snapshot", logger):
                account_snapshot: AccountSnapshot = self.broker.get_account_snapshot()
                option_buying_power_raw: Any = account_snapshot.options_buying_power
                equity_raw: Any = account_snapshot.equity

            with log_scope("broker.get_open_orders", logger):
                open_orders_raw: Any = self.broker.get_open_orders()

            with log_scope("broker.get_positions", logger):
                positions_raw: Any = self.broker.get_positions()

            option_buying_power: float = (
                float(option_buying_power_raw)
                if isinstance(option_buying_power_raw, (int, float))
                else 0.0
            )
            equity: float = float(equity_raw) if isinstance(equity_raw, (int, float)) else 0.0

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

            return _BaseSnapshot(
                as_of_utc=as_of_utc,
                option_buying_power=option_buying_power,
                equity=equity,
                open_orders=open_orders,
                positions=positions,
            )

    def _build_asset_quotes(self, *, symbols: Set[Symbol]) -> Dict[Symbol, AssetQuote]:
        """
        Fetch one quote per symbol (parameter driven).
        """
        with log_scope("orchestrator._build_asset_quotes", logger):
            quotes: Dict[Symbol, AssetQuote] = {}

            for sym in symbols:
                try:
                    with log_scope("broker.get_asset_quote", logger, extra=f"symbol={sym}"):
                        quote: AssetQuote = self.broker.get_asset_quote(str(sym))
                except KeyboardInterrupt:
                    logger.exception("KeyboardInterrupt during get_asset_quote for symbol=%s", sym)
                    raise
                except Exception as exc:
                    logger.exception("Failed to fetch quote for %s: %s", sym, exc)
                    continue

                quotes[sym] = quote

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

    def _build_option_chains(
        self,
        *,
        requests: List[OptionChainRequest],
        as_of_utc: datetime,
    ) -> Dict[Tuple[Symbol, str], List[Dict[str, Any]]]:
        """
        Fetch option chains only for the provided requests (parameter driven).
        """
        with log_scope("orchestrator._build_option_chains", logger):
            chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]] = {}
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

                chain_raw: List[Dict[str, Any]] = [r for r in chain_raw_any if isinstance(r, dict)]

                self._log_option_chain_preview(
                    underlying_sym=underlying_sym,
                    chain_raw=chain_raw,
                    as_of_utc=as_of_utc,
                    preview_rows=12,
                )

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

            logger.info("Option chains built | stored=%d", int(len(chains)))
            return chains

    def _count_pmcc_units_from_positions(self, *, underlying: Symbol, positions: List[Dict[str, Any]]) -> int:
        """
        Conservative PMCC unit counter.

        This is used for planning (skip expensive fetches) and must be robust.
        It should match the semantics used in RiskEngine as closely as possible.

        Assumptions (based on your current mocked positions):
        - options are represented as {"asset_class": "us_option", "symbol": "SPY260619C00060000"}
        - we count each unique long LEAP call as one unit
        - we do not attempt to pair long and short legs here
        """
        under: str = str(underlying).strip().upper()
        unique_long_calls: Set[str] = set()

        for p in positions:
            if not isinstance(p, dict):
                continue

            asset_class: str = str(p.get("asset_class", "")).strip().lower()
            if asset_class not in {"us_option", "option", "options"}:
                continue

            sym: str = str(p.get("symbol", "")).strip().upper()
            if not sym.startswith(under):
                continue

            qty_any: Any = p.get("qty")
            qty: Optional[float]
            try:
                qty = float(qty_any) if qty_any is not None else None
            except Exception:
                qty = None

            # If qty is missing (your TBOT_TEST_POSITIONS_JSON), treat as long for planning only.
            is_long: bool = True if qty is None else (qty > 0)

            if not is_long:
                continue

            # Only calls count towards a PMCC unit in this conservative planner.
            if "C" in sym[-9:]:
                unique_long_calls.add(sym)

        return int(len(unique_long_calls))

    def _build_cycle_plan(self, *, base: _BaseSnapshot) -> CyclePlan:
        """
        Create a CyclePlan that decides which expensive IO we do this cycle.
        """
        with log_scope("orchestrator._build_cycle_plan", logger):
            strategy_underlyings: Set[Symbol] = self._strategy_symbols()

            plan: CyclePlan = build_cycle_plan(
                strategy_underlyings=strategy_underlyings,
                open_orders=base.open_orders,
                positions=base.positions,
                max_pmcc_units_per_underlying=2,
                pmcc_unit_counter=lambda underlying, positions: self._count_pmcc_units_from_positions(
                    underlying=underlying, positions=positions
                ),
                skip_chain_if_open_order_exists=True,
                skip_chain_if_at_unit_cap=True,
            )

            # Log a compact plan summary for visibility.
            try:
                for p in plan.underlyings:
                    logger.info(
                        "CyclePlan | underlying=%s fetch_quote=%s fetch_chain=%s reasons=%s",
                        str(p.underlying),
                        str(bool(p.should_fetch_quote)),
                        str(bool(p.should_fetch_option_chain)),
                        ",".join(list(p.reasons)) if getattr(p, "reasons", None) else "<na>",
                    )
            except Exception:
                logger.info("CyclePlan built (no per-underlying detail available).")

            return plan

    def _build_context(self, *, base: _BaseSnapshot, plan: CyclePlan) -> RiskContext:
        """
        Build the final RiskContext snapshot for this cycle.

        Key rule:
        - we do not fetch option chains unless the plan says we should.
        """
        with log_scope("orchestrator._build_context", logger):
            # Quotes are relatively cheap; still plan driven.
            plan_underlyings: Set[Symbol] = set()
            for p in plan.underlyings:
                if bool(p.should_fetch_quote):
                    plan_underlyings.add(p.underlying)

            asset_quotes: Dict[Symbol, AssetQuote] = self._build_asset_quotes(symbols=plan_underlyings)

            # Option chains are expensive; filter requests using the plan.
            raw_requests: List[OptionChainRequest] = self._collect_option_chain_requests()

            allowed_underlyings_for_chain: Set[Symbol] = set()
            for p in plan.underlyings:
                if bool(p.should_fetch_option_chain):
                    allowed_underlyings_for_chain.add(p.underlying)

            filtered_requests: List[OptionChainRequest] = []
            for r in raw_requests:
                try:
                    u: Symbol = normalise_symbol(r.underlying)
                except Exception:
                    continue

                if u in allowed_underlyings_for_chain:
                    filtered_requests.append(r)

            option_chains: Dict[Tuple[Symbol, str], List[Dict[str, Any]]]
            if filtered_requests:
                option_chains = self._build_option_chains(requests=filtered_requests, as_of_utc=base.as_of_utc)
            else:
                option_chains = {}
                logger.info("Skipping option chain fetch | reason=cycle_plan_filtered_all_requests")

            price_policy: PriceSelectionPolicy = DefaultPriceSelectionPolicy()

            ctx: RiskContext = RiskContext(
                as_of_utc=base.as_of_utc,
                option_buying_power=base.option_buying_power,
                equity=base.equity,
                positions=base.positions,
                open_orders=base.open_orders,
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
        with log_scope("orchestrator._collect_intents", logger):
            all_intents: List[TradeIntent] = []

            for strat in self.strategies:
                try:
                    with log_scope("strategy.generate_intents", logger, extra=f"strategy_id={strat.strategy_id}"):
                        intents: List[TradeIntent] = strat.generate_intents(ctx)
                except Exception as exc:
                    logger.exception("Strategy %s failed to generate intents: %s", strat.strategy_id, exc)
                    continue

                logger.info("Strategy %s produced intents=%d", strat.strategy_id, int(len(intents)))
                all_intents.extend(intents)

            logger.info("Total intents collected=%d", int(len(all_intents)))
            return all_intents

    def _log_decisions(self, decisions: List[RiskDecision]) -> None:
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

                logger.warning("RiskDecision invalid (no approved or rejected).")

    def run_cycle(self) -> None:
        """
        Execute one full orchestrator cycle.

        New flow (fixes your complaint):
        - Fetch base broker state first (account, open_orders, positions).
        - Build a CyclePlan (pure).
        - Fetch quotes and option chains only if the plan says so.
        - Build final RiskContext.
        - Strategies -> intents -> risk decisions.
        - Submit approved orders (unless dry_run).
        """
        with log_scope("orchestrator.run_cycle", logger):
            base: _BaseSnapshot = self._build_base_snapshot()
            plan: CyclePlan = self._build_cycle_plan(base=base)
            ctx: RiskContext = self._build_context(base=base, plan=plan)

            logger.info(
                "Snapshot as_of_utc=%s option_buying_power=%s equity=%s open_orders=%s positions=%s asset_quotes=%s option_chains=%s",
                ctx.as_of_utc.isoformat(),
                ctx.option_buying_power,
                ctx.equity,
                len(ctx.open_orders),
                len(ctx.positions),
                len(ctx.asset_quotes),
                len(ctx.option_chains),
            )

            intents: List[TradeIntent] = self._collect_intents(ctx)
            logger.info("Collected %s intents.", len(intents))

            with log_scope("risk_engine.evaluate", logger, extra=f"intents={len(intents)}"):
                decisions: List[RiskDecision] = self.risk_engine.evaluate(ctx, intents)

            logger.info("Risk engine produced %s decisions.", len(decisions))
            self._log_decisions(decisions)

            if self.dry_run:
                logger.info("Dry run enabled: no orders will be submitted.")
                return

            for decision in decisions:
                if decision.approved is None:
                    continue

                order = decision.approved.order
                try:
                    response = self.broker.submit_order(order)
                    logger.info("Submitted order %s: %s", order, response)
                except Exception as exc:
                    logger.error("Failed to submit order %s: %s", order, exc)
