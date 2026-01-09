from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from TradingBot.v2.context import RiskContext
from TradingBot.v2.domain.ids import make_intent_id
from TradingBot.v2.domain.types import IntentId, OptionChainRequest, StrategyId, Symbol, normalise_symbol
from TradingBot.v2.intents import (
    OrderSide,
    OptionLeg,
    PmccIntentPayload,
    SelectedOption,
    TradeIntent,
)
from TradingBot.v2.logger import setup_logger
from TradingBot.v2.logging_utils import log_scope
from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2

logger = setup_logger("PMCC Strategy")


def _parse_iso8601_utc(ts: Any) -> Optional[datetime]:
    """
    Parse ISO8601 timestamps produced by brokers, typically ending in 'Z'.

    Returns
    -------
    datetime | None
        Timezone-aware UTC datetime, or None if parsing fails.
    """
    if not isinstance(ts, str):
        return None
    s: str = ts.strip()
    if not s:
        return None
    try:
        # Supports "2026-01-07T21:14:50.927197Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_call_dte(contract_symbol: str, *, as_of_utc: datetime) -> Optional[int]:
    """
    Extract DTE from OCC/OSI option symbol suffix.

    Expected suffix (last 15 chars):
    YYMMDD + (C/P) + strike(8 digits)
    """
    try:
        suffix: str = contract_symbol[-15:]
        yymmdd: str = suffix[0:6]
        cp: str = suffix[6:7]
        if cp != "C":
            return None

        expiry_utc: datetime = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=timezone.utc)
        dte: int = int((expiry_utc - as_of_utc).total_seconds() // 86400)
        return dte
    except Exception:
        return None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


@dataclass(frozen=True)
class _Candidate:
    symbol: str
    bid: float
    ask: float
    delta: float
    dte: int
    spread_abs: float
    spread_pct: float
    score: float

    # Metadata carried through into SelectedOption
    feed: str
    chain_newest_ts_utc: Optional[datetime]


@dataclass(frozen=True)
class PmccStrategyV2(StrategyV2):
    """
    Poor Man's Covered Call (PMCC) strategy (V2).

    Constraints
    - Strategy is pure: reads only from RiskContext, performs no broker IO.
    - Strategy selects contracts only, it does not size or submit.
    """

    underlying_symbol: str

    leap_min_dte: int = 365
    leap_max_dte: int = 545
    leap_target_delta: float = 0.80

    near_min_dte: int = 21
    near_max_dte: int = 45
    near_target_delta: float = 0.20

    _strategy_id: StrategyId = StrategyId("pmcc_v2")

    @property
    def strategy_id(self) -> StrategyId:
        return self._strategy_id

    @property
    def symbols(self) -> Sequence[Symbol]:
        with log_scope("pmcc.symbols", logger, extra=f"underlying_symbol={self.underlying_symbol}"):
            syms: Sequence[Symbol] = (normalise_symbol(self.underlying_symbol),)
            logger.info("Symbols declared | symbols=%s", [str(s) for s in syms])
            return syms

    @property
    def requires_option_chain(self) -> bool:
        return True

    @property
    def option_chain_symbols(self) -> Sequence[Symbol]:
        return self.symbols

    def option_chain_requests(self) -> List[OptionChainRequest]:
        """
        Tell the orchestrator which option chains to fetch.

        Freshness policy (max_age_seconds) is orchestrator-owned, not strategy-owned.
        """
        underlying: str = str(self.underlying_symbol).strip().upper()
        if not underlying:
            return []

        today: date = date.today()

        leap_exp_gte: date = today + timedelta(days=int(self.leap_min_dte))
        leap_exp_lte: date = today + timedelta(days=int(self.leap_max_dte))

        near_exp_gte: date = today + timedelta(days=int(self.near_min_dte))
        near_exp_lte: date = today + timedelta(days=int(self.near_max_dte))

        return [
            OptionChainRequest(
                request_id="pmcc_leap",
                underlying=underlying,
                include_calls=True,
                include_puts=False,
                feed="indicative",
                limit=0,
                expiration_date_gte=leap_exp_gte,
                expiration_date_lte=leap_exp_lte,
            ),
            OptionChainRequest(
                request_id="pmcc_near",
                underlying=underlying,
                include_calls=True,
                include_puts=False,
                feed="indicative",
                limit=0,
                expiration_date_gte=near_exp_gte,
                expiration_date_lte=near_exp_lte,
            ),
        ]

    def _select_candidate(
        self,
        *,
        ctx: RiskContext,
        underlying: Symbol,
        chain: List[Dict[str, Any]],
        leg_name: str,
        target_delta: float,
        dte_min: int,
        dte_max: int,
    ) -> Optional[_Candidate]:
        best: Optional[_Candidate] = None

        for row in chain:
            contract_symbol_any: Any = row.get("contract_symbol")
            if not isinstance(contract_symbol_any, str):
                continue

            contract_symbol: str = contract_symbol_any.strip().upper()
            if not contract_symbol:
                continue

            dte: Optional[int] = _parse_call_dte(contract_symbol, as_of_utc=ctx.as_of_utc)
            if dte is None or dte < int(dte_min) or dte > int(dte_max):
                continue

            latest_quote_any: Any = row.get("latestQuote")
            if not isinstance(latest_quote_any, dict):
                continue

            bid: Optional[float] = _to_float(latest_quote_any.get("bp"))
            ask: Optional[float] = _to_float(latest_quote_any.get("ap"))
            if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or ask < bid:
                continue

            spread_abs: float = float(ask - bid)
            mid: float = float((ask + bid) / 2.0)
            spread_pct: float = float(spread_abs / mid) if mid > 0.0 else 1.0

            greeks_any: Any = row.get("greeks")
            if not isinstance(greeks_any, dict):
                continue

            delta: Optional[float] = _to_float(greeks_any.get("delta"))
            if delta is None:
                continue

            delta_dist: float = float(abs(delta - float(target_delta)))
            score: float = float(delta_dist + 0.10 * spread_pct + 0.001 * spread_abs)

            feed_any: Any = row.get("_feed")
            feed: str = str(feed_any).strip().lower() if isinstance(feed_any, str) else "indicative"
            if feed not in {"opra", "indicative"}:
                feed = "indicative"

            newest_ts_utc: Optional[datetime] = _parse_iso8601_utc(row.get("_newest_ts"))

            cand: _Candidate = _Candidate(
                symbol=contract_symbol,
                bid=float(bid),
                ask=float(ask),
                delta=float(delta),
                dte=int(dte),
                spread_abs=float(spread_abs),
                spread_pct=float(spread_pct),
                score=float(score),
                feed=feed,
                chain_newest_ts_utc=newest_ts_utc,
            )

            if best is None or cand.score < best.score:
                best = cand

        if best is None:
            logger.warning(
                "No %s candidate found | underlying=%s target_delta=%.3f dte_range=%d-%d",
                leg_name,
                str(underlying),
                float(target_delta),
                int(dte_min),
                int(dte_max),
            )
            return None

        logger.info(
            "Selected %s candidate | underlying=%s symbol=%s dte=%d bid=%.6f ask=%.6f spread_abs=%.6f spread_pct=%.4f delta=%.4f score=%.6f feed=%s newest_ts=%s",
            leg_name,
            str(underlying),
            best.symbol,
            int(best.dte),
            float(best.bid),
            float(best.ask),
            float(best.spread_abs),
            float(best.spread_pct),
            float(best.delta),
            float(best.score),
            best.feed,
            best.chain_newest_ts_utc.isoformat() if best.chain_newest_ts_utc else None,
        )
        return best

    def generate_intents(self, ctx: RiskContext) -> List[TradeIntent]:
        with log_scope("pmcc.generate_intents", logger, extra=f"underlying_symbol={self.underlying_symbol}"):
            underlying: Symbol = normalise_symbol(self.underlying_symbol)

            leap_chain: List[Dict[str, Any]] = ctx.option_chains.get((underlying, "pmcc_leap"), [])
            near_chain: List[Dict[str, Any]] = ctx.option_chains.get((underlying, "pmcc_near"), [])

            if not leap_chain:
                logger.warning(
                    "No LEAP option chain available in context | underlying=%s key=%s",
                    str(underlying),
                    "pmcc_leap",
                )
                return []
            if not near_chain:
                logger.warning(
                    "No NEAR option chain available in context | underlying=%s key=%s",
                    str(underlying),
                    "pmcc_near",
                )
                return []

            leap: Optional[_Candidate] = self._select_candidate(
                ctx=ctx,
                underlying=underlying,
                chain=leap_chain,
                leg_name="LEAP",
                target_delta=float(self.leap_target_delta),
                dte_min=int(self.leap_min_dte),
                dte_max=int(self.leap_max_dte),
            )
            if leap is None:
                return []

            near: Optional[_Candidate] = self._select_candidate(
                ctx=ctx,
                underlying=underlying,
                chain=near_chain,
                leg_name="NEAR",
                target_delta=float(self.near_target_delta),
                dte_min=int(self.near_min_dte),
                dte_max=int(self.near_max_dte),
            )
            if near is None:
                return []

            intent_id: IntentId = make_intent_id(
                strategy_id=self.strategy_id,
                underlying=underlying,
                as_of_utc=ctx.as_of_utc,
            )

            leap_sel: SelectedOption = SelectedOption(
                option_symbol=leap.symbol,
                ask_price=float(leap.ask),
                bid_price=float(leap.bid),
                delta=float(leap.delta),
                dte=int(leap.dte),
                feed=leap.feed,
                chain_newest_ts_utc=leap.chain_newest_ts_utc,
            )

            near_sel: SelectedOption = SelectedOption(
                option_symbol=near.symbol,
                ask_price=float(near.ask),
                bid_price=float(near.bid),
                delta=float(near.delta),
                dte=int(near.dte),
                feed=near.feed,
                chain_newest_ts_utc=near.chain_newest_ts_utc,
            )

            payload: PmccIntentPayload = PmccIntentPayload(
                underlying_symbol=underlying,
                leap_leg=OptionLeg(contract=leap_sel, side=OrderSide.BUY, ratio=1),
                near_leg=OptionLeg(contract=near_sel, side=OrderSide.SELL, ratio=1),
            )

            tags: Tuple[str, ...] = ("v2", "pmcc", "mleg")

            intent: TradeIntent = TradeIntent(
                intent_id=intent_id,
                strategy_id=self.strategy_id,
                symbol=underlying,
                payload=payload,
                time_in_force="day",
                tags=tags,
            )

            logger.info(
                "PMCC intent produced | intent_id=%s underlying=%s leap=%s near=%s",
                str(intent.intent_id),
                str(underlying),
                leap_sel.option_symbol,
                near_sel.option_symbol,
            )

            return [intent]





# '''
# A Poor Man's Covered Call (PMCC) is essentiall:

# Long deep-in-the-money LEAP call:
# -High delta (≈0.75-0.90)
# -Long duration (1-2 years)

# Short near-dated out-of-the-money call:
# -Harvests theta
# -Repeatedly sold 

# So the strategy wants:
# -High option premiums
# -Predictable theta decay in short calls

# Enough movement to reset strikes

# '''

# from __future__ import annotations

# # dataclass is used to reduce boilerplate for immutable strategy objects.
# from dataclasses import dataclass

# from datetime import datetime, timezone, timedelta, date

# # List, Sequence, and Tuple are used for explicit typing of strategy outputs.
# from typing import List, Sequence, Tuple, Dict, Any, Optional

# # RiskContext provides the immutable, per-cycle snapshot of account and market state.
# from TradingBot.v2.context import RiskContext

# # Helper to construct deterministic intent identifiers.
# from TradingBot.v2.domain.ids import make_intent_id

# # Domain types used by the strategy.
# from TradingBot.v2.domain.types import IntentId, StrategyId, Symbol, normalise_symbol, OptionChainRequest

# # Intent and payload objects emitted by the strategy.
# from TradingBot.v2.intents import (
#     OrderSide,
#     OptionLeg,
#     PmccIntentPayload,
#     SelectedOption,
#     TradeIntent,
# )

# # PriceSide defines BUY vs SELL semantics for execution-aware pricing.
# from TradingBot.v2.risk.price_policy import PriceSide

# # Base interface all V2 strategies must implement.
# from TradingBot.v2.strategies.strategy_interface_v2 import StrategyV2

# # Logging utilities for consistent structured logs.
# from TradingBot.v2.logger import setup_logger
# from TradingBot.v2.logging_utils import log_scope

# # Module-level logger.
# logger = setup_logger("PMCC Strategy")


# @dataclass(frozen=True)
# class PmccStrategyV2(StrategyV2):
#     """
#     Poor Man's Covered Call (PMCC) strategy (V2).

#     High-level intent
#     - Enter a long-dated call option (LEAP) on the underlying.
#     - Sell shorter-dated call options against that LEAP.

#     Architectural constraints
#     - This strategy must be pure with respect to broker IO.
#     - All market data is accessed exclusively via RiskContext.
#     - No sizing, approval, or submission logic exists here.
#     """

#     # Underlying equity symbol, provided as user input.
#     underlying_symbol: str

#     # PMCC configuration (strategy-owned targets)
#     leap_min_dte: int = 365
#     leap_max_dte: int = 545
#     leap_target_delta: float = 0.80

#     near_min_dte: int = 21
#     near_max_dte: int = 45
#     near_target_delta: float = 0.20

#     # Stable strategy identifier used in intent IDs and logs.
#     _strategy_id: StrategyId = StrategyId("pmcc_v2")

#     @property
#     def strategy_id(self) -> StrategyId:
#         """
#         Return the stable identifier for this strategy.

#         Why this exists
#         - Allows orchestrator and risk engine to attribute intents
#           to a specific strategy without relying on class names.
#         """
#         return self._strategy_id

#     @property
#     def symbols(self) -> Sequence[Symbol]:
#         """
#         Declare the set of underlying symbols required by this strategy.

#         Why this exists
#         - The orchestrator uses this to prefetch quotes once per cycle.
#         - Prevents strategies from calling the broker directly.
#         """
#         with log_scope("pmcc.symbols", logger, extra=f"underlying_symbol={self.underlying_symbol}"):
#             syms: Sequence[Symbol] = (normalise_symbol(self.underlying_symbol),)
#             logger.info("Symbols declared | symbols=%s", [str(s) for s in syms])
#             return syms

#     @property
#     def requires_option_chain(self) -> bool:
#         """
#         Declare that PMCC requires option-chain data.
#         """
#         return True

#     @property
#     def option_chain_symbols(self) -> Sequence[Symbol]:
#         """
#         Declare which underlyings require option chains for this strategy.
#         """
#         return self.symbols

#     def option_chain_requests(self) -> List[OptionChainRequest]:
#         """
#         Tell the orchestrator exactly which option contracts we want fetched.

#         We deliberately do not set max_age_seconds here.
#         Freshness is orchestrator policy.
#         """
#         underlying: str = str(self.underlying_symbol).strip().upper()
#         if not underlying:
#             return []

#         today: date = date.today()

#         leap_exp_gte: date = today + timedelta(days=int(self.leap_min_dte))
#         leap_exp_lte: date = today + timedelta(days=int(self.leap_max_dte))

#         near_exp_gte: date = today + timedelta(days=int(self.near_min_dte))
#         near_exp_lte: date = today + timedelta(days=int(self.near_max_dte))

#         leap_req: OptionChainRequest = OptionChainRequest(
#             request_id="pmcc_leap",
#             underlying=underlying,
#             include_calls=True,
#             include_puts=False,
#             feed="indicative",
#             limit=0,
#             expiration_date_gte=leap_exp_gte,
#             expiration_date_lte=leap_exp_lte,
#         )

#         near_req: OptionChainRequest = OptionChainRequest(
#             request_id="pmcc_near",
#             underlying=underlying,
#             include_calls=True,
#             include_puts=False,
#             feed="indicative",
#             limit=0,
#             expiration_date_gte=near_exp_gte,
#             expiration_date_lte=near_exp_lte,
#         )

#         return [leap_req, near_req]

#     def generate_intents(self, ctx: RiskContext) -> List[TradeIntent]:
#         """
#         Generate PMCC trade intents for the current cycle.

#         Reads option chains from RiskContext using keys:
#         - (underlying, "pmcc_leap")
#         - (underlying, "pmcc_near")
#         """
#         with log_scope("pmcc.generate_intents", logger, extra=f"underlying_symbol={self.underlying_symbol}"):
#             underlying: Symbol = normalise_symbol(self.underlying_symbol)

#             leap_chain: List[Dict[str, Any]] = ctx.option_chains.get((underlying, "pmcc_leap"), [])
#             near_chain: List[Dict[str, Any]] = ctx.option_chains.get((underlying, "pmcc_near"), [])

#             if not leap_chain:
#                 logger.warning("No LEAP option chain available in context | underlying=%s key=%s", str(underlying), "pmcc_leap")
#                 return []
#             if not near_chain:
#                 logger.warning("No NEAR option chain available in context | underlying=%s key=%s", str(underlying), "pmcc_near")
#                 return []

#             def _parse_call_and_dte(contract_symbol: str, *, as_of_utc: datetime) -> Optional[int]:
#                 """
#                 Extract DTE (days to expiry) from OCC option symbol suffix.

#                 Expected OCC suffix layout (last 15 chars):
#                 - YYMMDD + (C/P) + strike(8 digits)
#                 """
#                 try:
#                     suffix: str = contract_symbol[-15:]
#                     yymmdd: str = suffix[0:6]
#                     cp: str = suffix[6:7]
#                     if cp != "C":
#                         return None

#                     expiry_utc: datetime = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=timezone.utc)
#                     dte: int = int((expiry_utc - as_of_utc).total_seconds() // 86400)
#                     return dte
#                 except Exception:
#                     return None

#             def _to_float(v: Any) -> Optional[float]:
#                 try:
#                     return float(v)
#                 except Exception:
#                     return None

#             @dataclass(frozen=True)
#             class _Candidate:
#                 symbol: str
#                 bid: float
#                 ask: float
#                 delta: float
#                 dte: int
#                 spread_abs: float
#                 spread_pct: float
#                 score: float

#             def _select_candidate(
#                 *,
#                 chain: List[Dict[str, Any]],
#                 leg_name: str,
#                 target_delta: float,
#                 dte_min: int,
#                 dte_max: int,
#                 max_spread_pct: float,
#                 max_spread_abs: float,
#             ) -> Optional[_Candidate]:
#                 best: Optional[_Candidate] = None

#                 for row in chain:
#                     contract_symbol_any: Any = row.get("contract_symbol")
#                     if not isinstance(contract_symbol_any, str):
#                         continue

#                     contract_symbol: str = contract_symbol_any.strip().upper()
#                     if not contract_symbol:
#                         continue

#                     dte: Optional[int] = _parse_call_and_dte(contract_symbol, as_of_utc=ctx.as_of_utc)
#                     if dte is None:
#                         continue
#                     if dte < int(dte_min) or dte > int(dte_max):
#                         continue

#                     latest_quote_any: Any = row.get("latestQuote")
#                     if not isinstance(latest_quote_any, dict):
#                         continue

#                     bid: Optional[float] = _to_float(latest_quote_any.get("bp"))
#                     ask: Optional[float] = _to_float(latest_quote_any.get("ap"))
#                     if bid is None or ask is None:
#                         continue
#                     if bid <= 0.0 or ask <= 0.0:
#                         continue
#                     if ask < bid:
#                         continue

#                     spread_abs: float = float(ask - bid)
#                     mid: float = float((ask + bid) / 2.0)
#                     spread_pct: float = float(spread_abs / mid) if mid > 0.0 else 1.0

#                     if spread_abs > float(max_spread_abs):
#                         continue
#                     if spread_pct > float(max_spread_pct):
#                         continue

#                     greeks_any: Any = row.get("greeks")
#                     if not isinstance(greeks_any, dict):
#                         continue

#                     delta: Optional[float] = _to_float(greeks_any.get("delta"))
#                     if delta is None:
#                         continue

#                     delta_dist: float = float(abs(delta - float(target_delta)))
#                     score: float = float(delta_dist + 0.10 * spread_pct + 0.001 * spread_abs)

#                     cand: _Candidate = _Candidate(
#                         symbol=contract_symbol,
#                         bid=float(bid),
#                         ask=float(ask),
#                         delta=float(delta),
#                         dte=int(dte),
#                         spread_abs=float(spread_abs),
#                         spread_pct=float(spread_pct),
#                         score=float(score),
#                     )

#                     if best is None or cand.score < best.score:
#                         best = cand

#                 if best is None:
#                     logger.warning(
#                         "No %s candidate found | underlying=%s target_delta=%.3f dte_range=%d-%d",
#                         leg_name,
#                         str(underlying),
#                         float(target_delta),
#                         int(dte_min),
#                         int(dte_max),
#                     )
#                     return None

#                 logger.info(
#                     "Selected %s candidate | underlying=%s symbol=%s dte=%d bid=%.6f ask=%.6f spread_abs=%.6f spread_pct=%.4f delta=%.4f score=%.6f",
#                     leg_name,
#                     str(underlying),
#                     best.symbol,
#                     int(best.dte),
#                     float(best.bid),
#                     float(best.ask),
#                     float(best.spread_abs),
#                     float(best.spread_pct),
#                     float(best.delta),
#                     float(best.score),
#                 )
#                 return best

#             leap: Optional[_Candidate] = _select_candidate(
#                 chain=leap_chain,
#                 leg_name="LEAP",
#                 target_delta=float(self.leap_target_delta),
#                 dte_min=int(self.leap_min_dte),
#                 dte_max=int(self.leap_max_dte),
#                 max_spread_pct=0.25,
#                 max_spread_abs=5.00,
#             )
#             if leap is None:
#                 return []

#             near: Optional[_Candidate] = _select_candidate(
#                 chain=near_chain,
#                 leg_name="NEAR",
#                 target_delta=float(self.near_target_delta),
#                 dte_min=int(self.near_min_dte),
#                 dte_max=int(self.near_max_dte),
#                 max_spread_pct=0.35,
#                 max_spread_abs=2.50,
#             )
#             if near is None:
#                 return []

#             intent_id: IntentId = make_intent_id(
#                 strategy_id=self.strategy_id,
#                 underlying=underlying,
#                 as_of_utc=ctx.as_of_utc,
#             )

#             leap_sel: SelectedOption = SelectedOption(
#                 option_symbol=leap.symbol,
#                 ask_price=float(leap.ask),
#                 bid_price=float(leap.bid),
#                 delta=float(leap.delta),
#                 dte=int(leap.dte),
#             )

#             near_sel: SelectedOption = SelectedOption(
#                 option_symbol=near.symbol,
#                 ask_price=float(near.ask),
#                 bid_price=float(near.bid),
#                 delta=float(near.delta),
#                 dte=int(near.dte),
#             )

#             leap_leg: OptionLeg = OptionLeg(
#                 contract=leap_sel,
#                 side=OrderSide.BUY,
#                 ratio=1,
#             )

#             near_leg: OptionLeg = OptionLeg(
#                 contract=near_sel,
#                 side=OrderSide.SELL,
#                 ratio=1,
#             )

#             payload: PmccIntentPayload = PmccIntentPayload(
#                 underlying_symbol=underlying,
#                 leap_leg=leap_leg,
#                 near_leg=near_leg,
#             )

#             tags: Tuple[str, ...] = ("v2", "pmcc", "mleg")

#             intent: TradeIntent = TradeIntent(
#                 intent_id=intent_id,
#                 strategy_id=self.strategy_id,
#                 symbol=underlying,
#                 payload=payload,
#                 time_in_force="day",
#                 tags=tags,
#             )

#             logger.info(
#                 "PMCC intent produced | intent_id=%s underlying=%s leap=%s near=%s",
#                 str(intent.intent_id),
#                 str(underlying),
#                 leap_sel.option_symbol,
#                 near_sel.option_symbol,
#             )

#             return [intent]



