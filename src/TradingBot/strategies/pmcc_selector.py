from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from TradingBot.config.strategy_config import PmccConfig
from TradingBot.domain.intents import SelectedOption
from TradingBot.domain.types import Symbol
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("PMCC Selector")


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
    """
    Alpaca OCC option symbols end with YYMMDDC/P########.
    We only handle calls here (C).
    """
    try:
        suffix: str = contract_symbol[-15:]
        yymmdd: str = suffix[0:6]
        cp: str = suffix[6:7]
        if cp != "C":
            return None

        expiry_utc: datetime = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=timezone.utc)
        dte: int = int((expiry_utc - as_of_utc.astimezone(timezone.utc)).total_seconds() // 86400)
        return dte
    except Exception:
        return None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


@dataclass(frozen=True)
class Candidate:
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
class PmccContractSelector:
    """
    Pure selector: picks contracts from chain rows. No sizing, no broker IO.
    """

    cfg: PmccConfig

    def select_near_any(self, *, chain: List[Dict[str, Any]]) -> Optional[str]:
        for row in chain:
            sym_any: Any = row.get("contract_symbol")
            if isinstance(sym_any, str) and sym_any.strip():
                return sym_any.strip().upper()
        return None

    def select_leg(
        self,
        *,
        chain: List[Dict[str, Any]],
        leg_name: str,
        target_delta: float,
        dte_min: int,
        dte_max: int,
        as_of_utc: datetime,
    ) -> Optional[SelectedOption]:
        cand: Optional[Candidate] = self._select_candidate(
            chain=chain,
            leg_name=leg_name,
            target_delta=target_delta,
            dte_min=dte_min,
            dte_max=dte_max,
            as_of_utc=as_of_utc,
        )
        if cand is None:
            return None
        return self._to_selected_option(cand)

    def _to_selected_option(self, cand: Candidate) -> SelectedOption:
        return SelectedOption(
            option_symbol=cand.symbol,
            ask_price=float(cand.ask),
            bid_price=float(cand.bid),
            delta=float(cand.delta),
            dte=int(cand.dte),
            feed=str(cand.feed),
            chain_newest_ts_utc=cand.chain_newest_ts_utc,
        )

    def _select_candidate(
        self,
        *,
        chain: List[Dict[str, Any]],
        leg_name: str,
        target_delta: float,
        dte_min: int,
        dte_max: int,
        as_of_utc: datetime,
    ) -> Optional[Candidate]:
        best: Optional[Candidate] = None

        with log_scope(
            "pmcc_selector.select_candidate",
            logger,
            extra=f"leg={leg_name} target_delta={target_delta:.3f} dte={dte_min}-{dte_max}",
        ):
            for row in chain:
                contract_symbol_any: Any = row.get("contract_symbol")
                if not isinstance(contract_symbol_any, str):
                    continue

                contract_symbol: str = contract_symbol_any.strip().upper()
                if not contract_symbol:
                    continue

                dte: Optional[int] = _parse_call_dte(contract_symbol, as_of_utc=as_of_utc)
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

                cand = Candidate(
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
                logger.warning("No candidate found | leg=%s", leg_name)
                return None

            logger.info(
                "Selected candidate | leg=%s symbol=%s dte=%d bid=%.6f ask=%.6f spread_pct=%.4f delta=%.4f score=%.6f",
                leg_name,
                best.symbol,
                int(best.dte),
                float(best.bid),
                float(best.ask),
                float(best.spread_pct),
                float(best.delta),
                float(best.score),
            )
            return best
