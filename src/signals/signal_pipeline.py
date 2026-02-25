from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from src.domain.signals import IvRegimeSignal, SignalSnapshot
from src.utilities.logger import setup_logger

logger = setup_logger("SignalPipeline")

_IV_HISTORY_DAYS = 252


class SignalPipelineABC(ABC):
    @abstractmethod
    def compute(self, snapshot) -> SignalSnapshot:
        raise NotImplementedError


class DefaultSignalPipeline(SignalPipelineABC):
    """No-op pipeline — returns empty signals."""
    def compute(self, snapshot) -> SignalSnapshot:
        return SignalSnapshot.empty(as_of_utc=snapshot.as_of_utc)


@dataclass
class IvRegimePipeline(SignalPipelineABC):
    """
    Computes IV Rank and classifies the volatility regime each cycle.

    IV source: ATM ~30-DTE call IV from the option chain already in the snapshot.
    If greeks.iv is present on the contract row, use it directly. Otherwise
    estimate IV from the mid price using the Brenner-Subrahmanyam approximation:
      IV ≈ (mid / spot) * sqrt(2π / T)

    IVR = (iv_current - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100

    History is maintained in a rolling in-memory buffer (capped at 252 entries,
    one per trading day). Regime is UNKNOWN until at least 5 data points exist.

    HV30 (optional): injected via IvRegimePipelineWithBars which also calls
    broker.get_daily_bars() to compute realised vol.
    """

    underlying: str = "SPY"
    _iv_history: List[Tuple[str, float]] = field(default_factory=list, init=False)

    def compute(self, snapshot) -> SignalSnapshot:
        from src.orchestration.cycle_snapshot import CycleSnapshot
        sym        = self.underlying.strip().upper()
        iv_current = self._estimate_atm_iv(snapshot, sym)
        hv30       = self._compute_hv30(snapshot, sym)

        if iv_current is None:
            logger.warning("IV regime: could not estimate ATM IV for %s — UNKNOWN", sym)
            return SignalSnapshot(
                as_of_utc=snapshot.as_of_utc,
                iv_regime=IvRegimeSignal.unknown(),
            )

        # Record once per day
        date_key = snapshot.as_of_utc.strftime("%Y-%m-%d")
        if not self._iv_history or self._iv_history[-1][0] != date_key:
            self._iv_history.append((date_key, iv_current))
            if len(self._iv_history) > _IV_HISTORY_DAYS:
                self._iv_history.pop(0)

        iv_values = [v for _, v in self._iv_history]

        if len(iv_values) < 5:
            logger.info(
                "IV regime: insufficient history (%d days) — UNKNOWN | iv_current=%.3f",
                len(iv_values), iv_current,
            )
            return SignalSnapshot(
                as_of_utc=snapshot.as_of_utc,
                iv_regime=IvRegimeSignal.unknown(),
            )

        iv_52w_high = max(iv_values)
        iv_52w_low  = min(iv_values)
        ivr = (
            (iv_current - iv_52w_low) / (iv_52w_high - iv_52w_low) * 100
            if iv_52w_high > iv_52w_low else 50.0
        )

        signal = IvRegimeSignal.from_ivr(
            ivr=ivr,
            iv_current=iv_current,
            iv_52w_high=iv_52w_high,
            iv_52w_low=iv_52w_low,
            hv30=hv30,
        )

        logger.info(
            "IV regime | sym=%s regime=%s ivr=%.1f iv=%.3f hv30=%s iv_hv=%s history=%d days",
            sym, signal.regime.value, ivr, iv_current,
            f"{hv30:.3f}" if hv30 else "n/a",
            f"{signal.iv_hv_ratio:.2f}" if signal.iv_hv_ratio else "n/a",
            len(iv_values),
        )

        return SignalSnapshot(as_of_utc=snapshot.as_of_utc, iv_regime=signal)

    def _estimate_atm_iv(self, snapshot, sym: str) -> Optional[float]:
        spot = self._get_spot(snapshot, sym)
        if spot <= 0:
            return None

        best_iv    = None
        best_score = float("inf")

        for key, chain in snapshot.option_chains.items():
            if str(key[0]).upper() != sym:
                continue
            for row in chain:
                contract_sym = str(row.get("contract_symbol", "")).upper()
                if not contract_sym:
                    continue
                try:
                    from src.risk.pmcc_sizer import parse_osi
                    parsed = parse_osi(contract_sym)
                    dte    = max(1, (parsed.expiry.date() - snapshot.as_of_utc.date()).days)
                    strike = float(parsed.strike)
                except Exception:
                    continue

                # Only near-ATM calls
                if abs(strike - spot) / spot > 0.05:
                    continue

                # Try greeks.iv
                greeks = row.get("greeks") or {}
                iv = None
                for key_name in ("iv", "impliedVolatility", "implied_volatility"):
                    v = greeks.get(key_name)
                    if v:
                        try:
                            iv = float(v)
                            break
                        except (TypeError, ValueError):
                            pass

                # Fallback: Brenner-Subrahmanyam approximation
                if not iv:
                    quote = row.get("latestQuote") or {}
                    ask   = float(quote.get("ap") or 0)
                    bid   = float(quote.get("bp") or 0)
                    mid   = (ask + bid) / 2 if ask > 0 and bid > 0 else 0
                    if mid > 0:
                        T  = dte / 365
                        iv = (mid / spot) * math.sqrt(2 * math.pi / T)

                if not iv or not (0.01 < iv < 2.0):
                    continue

                score = abs(dte - 30) / 30 + abs(strike - spot) / spot
                if score < best_score:
                    best_score = score
                    best_iv    = iv

        return best_iv

    def _compute_hv30(self, snapshot, sym: str) -> Optional[float]:
        # Base implementation returns None.
        # Use IvRegimePipelineWithBars for HV30.
        return None

    def _get_spot(self, snapshot, sym: str) -> float:
        for k, v in snapshot.asset_quotes.items():
            if str(k).upper() == sym:
                return v.mid or v.ask or v.bid or 0.0
        return 0.0


@dataclass
class IvRegimePipelineWithBars(IvRegimePipeline):
    """
    Extends IvRegimePipeline with HV30 computed from daily bars.

    Requires broker to be injected. Wire up in main.py:
      signal_pipeline=IvRegimePipelineWithBars(underlying="SPY", broker=broker)
    """
    broker: object = None  # BrokerABC — typed as object to avoid circular import

    def _compute_hv30(self, snapshot, sym: str) -> Optional[float]:
        if self.broker is None:
            return None
        try:
            bars_resp = self.broker.get_daily_bars(sym, lookback_days=35)
            if hasattr(bars_resp, "data"):
                bars = bars_resp.data.get(sym, [])
            elif isinstance(bars_resp, dict):
                bars = bars_resp.get(sym, [])
            else:
                bars = list(bars_resp) if bars_resp else []

            closes = []
            for bar in bars:
                c = getattr(bar, "close", None) or (bar.get("c") if isinstance(bar, dict) else None)
                if c:
                    closes.append(float(c))

            closes = closes[-22:]
            if len(closes) < 10:
                return None

            log_returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
            mean        = sum(log_returns) / len(log_returns)
            variance    = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
            return math.sqrt(variance) * math.sqrt(252)

        except Exception as exc:
            logger.warning("HV30 computation failed for %s: %s", sym, exc)
            return None