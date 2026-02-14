from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Literal

from TradingBot.config.strategy_config import PmccConfig
from TradingBot.domain.ids import make_intent_id
from TradingBot.domain.intents import (
    IntentOptionLeg,
    OptionIntentPayload,
    OrderSide,
    OptionLeg,
    PmccIntentPayload,
    PositionIntent,
    SelectedOption,
    TradeIntent,
)
from TradingBot.domain.orders import TimeInForce
from TradingBot.domain.types import (
    IntentId,
    OptionChainRequest,
    StrategyId,
    Symbol,
    normalise_symbol,
)
from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC
from TradingBot.strategies.option_chain_consumer import OptionChainConsumerABC
from TradingBot.strategies.strategy_interface import StrategyABC
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("PMCC Strategy")

PmccState = Literal["FLAT", "LEAP_ONLY", "COVERED", "BROKEN"]


def _parse_iso8601_utc(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    s: str = ts.strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_call_dte(contract_symbol: str, *, as_of_utc: datetime) -> Optional[int]:
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
    feed: str
    chain_newest_ts_utc: Optional[datetime]


@dataclass(frozen=True)
class PmccStrategy(StrategyABC, OptionChainConsumerABC):
    """
    Poor Man's Covered Call (PMCC) strategy.

    Constraints
    - Strategy is pure: reads only from CycleSnapshot, performs no broker IO.
    - Strategy selects contracts only, it does not size or submit.
    """

    config: PmccConfig
    _strategy_id: StrategyId = StrategyId("PMCC")

    @property
    def strategy_id(self) -> StrategyId:
        return self._strategy_id

    @property
    def symbols(self) -> Sequence[Symbol]:
        cfg: PmccConfig = self.config
        underlying: str = str(cfg.underlying_symbol).strip().upper()
        with log_scope("pmcc.symbols", logger, extra=f"underlying_symbol={underlying}"):
            syms: Sequence[Symbol] = (normalise_symbol(underlying),)
            logger.info("Symbols declared | symbols=%s", [str(s) for s in syms])
            return syms

    @property
    def requires_option_chain(self) -> bool:
        return True

    @property
    def option_chain_symbols(self) -> Sequence[Symbol]:
        return self.symbols

    def universe(self) -> Set[Symbol]:
        return set(self.symbols)

    def option_chain_requests(self) -> List[OptionChainRequest]:
        cfg: PmccConfig = self.config
        underlying: str = str(cfg.underlying_symbol).strip().upper()
        if not underlying:
            return []

        today: date = date.today()

        leap_exp_gte: date = today + timedelta(days=int(cfg.leap.dte_min))
        leap_exp_lte: date = today + timedelta(days=int(cfg.leap.dte_max))

        near_exp_gte: date = today + timedelta(days=int(cfg.short.dte_min))
        near_exp_lte: date = today + timedelta(days=int(cfg.short.dte_max))

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

    def _pmcc_management_enabled(self) -> bool:
        return os.getenv("PMCC_ENABLE_MANAGEMENT", "false").strip().lower() == "true"

    def _select_candidate(
        self,
        *,
        ctx: CycleSnapshotABC,
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

            dte: Optional[int] = _parse_call_dte(contract_symbol, as_of_utc=ctx.as_of_utc())
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

    def _select_near_contract_symbol(self, *, near_chain: List[Dict[str, Any]]) -> Optional[str]:
        for row in near_chain:
            sym_any: Any = row.get("contract_symbol")
            if isinstance(sym_any, str) and sym_any.strip():
                return sym_any.strip().upper()
        return None

    def _find_pmcc_legs_in_positions(
        self,
        *,
        positions: List[Dict[str, Any]],
        leap_chain: List[Dict[str, Any]],
        near_chain: List[Dict[str, Any]],
    ) -> Dict[str, Optional[str]]:
        leap_symbols: Set[str] = {
            str(c.get("contract_symbol")).strip().upper()
            for c in leap_chain
            if isinstance(c.get("contract_symbol"), str) and str(c.get("contract_symbol")).strip()
        }
        near_symbols: Set[str] = {
            str(c.get("contract_symbol")).strip().upper()
            for c in near_chain
            if isinstance(c.get("contract_symbol"), str) and str(c.get("contract_symbol")).strip()
        }

        held_leap: Optional[str] = None
        held_near: Optional[str] = None

        for p in positions:
            sym_any: Any = p.get("symbol")
            if not isinstance(sym_any, str) or not sym_any.strip():
                continue

            sym: str = sym_any.strip().upper()
            if sym in leap_symbols:
                held_leap = sym
            if sym in near_symbols:
                held_near = sym

        return {"leap": held_leap, "near": held_near}

    def generate_intents(self, ctx: CycleSnapshotABC) -> List[TradeIntent]:
        cfg: PmccConfig = self.config
        intents: List[TradeIntent] = []

        underlying_str: str = str(cfg.underlying_symbol).strip().upper()
        with log_scope("pmcc.generate_intents", logger, extra=f"underlying_symbol={underlying_str}"):
            underlying: Symbol = normalise_symbol(underlying_str)
            as_of_utc: datetime = ctx.as_of_utc()

            chains = ctx.option_chains()
            leap_chain: List[Dict[str, Any]] = chains.get((underlying, "pmcc_leap"), [])
            near_chain: List[Dict[str, Any]] = chains.get((underlying, "pmcc_near"), [])

            # Management logic behind a flag
            if self._pmcc_management_enabled():
                legs = self._find_pmcc_legs_in_positions(
                    positions=ctx.positions(),
                    leap_chain=leap_chain,
                    near_chain=near_chain,
                )
                held_leap: Optional[str] = legs.get("leap")
                held_near: Optional[str] = legs.get("near")

                # LEAP-only: sell a NEAR
                if held_leap is not None and held_near is None:
                    selected_near: Optional[str] = self._select_near_contract_symbol(near_chain=near_chain)
                    if selected_near is not None:
                        intents.append(
                            TradeIntent(
                                intent_id=IntentId.new(),
                                strategy_id=self.strategy_id,
                                symbol=underlying,
                                payload=OptionIntentPayload(
                                    underlying_symbol=underlying,
                                    leg=IntentOptionLeg(
                                        contract_symbol=selected_near,
                                        position_intent=PositionIntent.SELL_TO_OPEN,
                                        role="NEAR",
                                        qty=None,
                                    ),
                                ),
                                time_in_force=TimeInForce.DAY,
                                tags=("pmcc", "manage", "sell_near"),
                            )
                        )
                    return intents

                # Broken: buy back NEAR
                if held_leap is None and held_near is not None:
                    intents.append(
                        TradeIntent(
                            intent_id=IntentId.new(),
                            strategy_id=self.strategy_id,
                            symbol=underlying,
                            payload=OptionIntentPayload(
                                underlying_symbol=underlying,
                                leg=IntentOptionLeg(
                                    contract_symbol=str(held_near),
                                    position_intent=PositionIntent.BUY_TO_CLOSE,
                                    role="NEAR",
                                    qty=None,
                                ),
                            ),
                            time_in_force=TimeInForce.DAY,
                            tags=("pmcc", "manage", "buyback_near"),
                        )
                    )
                    return intents

                # Covered: decide roll using cfg.roll thresholds (next step expands this)
                if held_leap is not None and held_near is not None:
                    if self._should_roll_near(
                        held_near_symbol=str(held_near),
                        near_chain=near_chain,
                        as_of_utc=as_of_utc,
                    ):
                        logger.info(
                            "PMCC roll triggered | underlying=%s held_near=%s",
                            str(underlying),
                            str(held_near),
                        )
                    return intents

            # Entry logic
            if not leap_chain:
                logger.warning("No LEAP option chain available in context | underlying=%s key=%s", str(underlying), "pmcc_leap")
                return []
            if not near_chain:
                logger.warning("No NEAR option chain available in context | underlying=%s key=%s", str(underlying), "pmcc_near")
                return []

            leap: Optional[_Candidate] = self._select_candidate(
                ctx=ctx,
                underlying=underlying,
                chain=leap_chain,
                leg_name="LEAP",
                target_delta=float(cfg.leap.target_delta),
                dte_min=int(cfg.leap.dte_min),
                dte_max=int(cfg.leap.dte_max),
            )
            if leap is None:
                return []

            near: Optional[_Candidate] = self._select_candidate(
                ctx=ctx,
                underlying=underlying,
                chain=near_chain,
                leg_name="NEAR",
                target_delta=float(cfg.short.target_delta),
                dte_min=int(cfg.short.dte_min),
                dte_max=int(cfg.short.dte_max),
            )
            if near is None:
                return []

            intent_id: IntentId = make_intent_id(
                strategy_id=self.strategy_id,
                underlying=underlying,
                as_of_utc=as_of_utc,
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

            intent: TradeIntent = TradeIntent(
                intent_id=intent_id,
                strategy_id=self.strategy_id,
                symbol=underlying,
                payload=payload,
                time_in_force=TimeInForce.DAY,
                tags=("pmcc", "entry", "mleg"),
            )

            logger.info(
                "PMCC intent produced | intent_id=%s underlying=%s leap=%s near=%s",
                str(intent.intent_id),
                str(underlying),
                leap_sel.option_symbol,
                near_sel.option_symbol,
            )
            return [intent]

    def _should_roll_near(
        self,
        held_near_symbol: str,
        near_chain: List[Dict[str, Any]],
        *,
        as_of_utc: datetime,
    ) -> bool:
        cfg: PmccConfig = self.config
        roll = cfg.roll

        held_sym: str = str(held_near_symbol).strip().upper()
        if not held_sym:
            return False

        for row in near_chain:
            sym_any: Any = row.get("contract_symbol")
            if not isinstance(sym_any, str) or not sym_any.strip():
                continue

            sym: str = sym_any.strip().upper()
            if sym != held_sym:
                continue

            dte_opt: Optional[int] = _parse_call_dte(sym, as_of_utc=as_of_utc)
            if dte_opt is not None and int(dte_opt) <= int(roll.dte_threshold):
                return True

            greeks_any: Any = row.get("greeks")
            if isinstance(greeks_any, dict):
                delta: Optional[float] = _to_float(greeks_any.get("delta"))
                if delta is not None and abs(float(delta)) >= float(roll.delta_threshold):
                    return True

            # Profit trigger needs entry credit/price tracking; not implemented yet.
            return False

        return False
