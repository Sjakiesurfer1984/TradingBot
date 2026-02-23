from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config.yaml_config import PmccLeapConfig, PmccLiquidityConfig, PmccShortConfig
from src.domain.intents import SelectedOption
from src.domain.orders import OptionContract, OptionRight
from src.risk.pmcc_sizer import parse_osi
from src.utilities.logger import setup_logger

logger = setup_logger("PmccContractSelector")


def _dte(expiry: datetime) -> int:
    return max(0, (expiry - datetime.now(timezone.utc).replace(tzinfo=None)).days)


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _spread_pct(bid: float, ask: float) -> Optional[float]:
    """Bid-ask spread as a fraction of mid. Returns None if prices are unusable."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid > 0 else None


@dataclass(frozen=True)
class ContractCandidate:
    option_symbol: str
    contract:      OptionContract
    dte:           int
    delta:         float
    ask:           float
    bid:           float
    mid:           float

    @property
    def strike(self) -> float:
        return float(self.contract.strike)

    @property
    def spread_pct(self) -> Optional[float]:
        return _spread_pct(self.bid, self.ask)


@dataclass
class PmccContractSelector:
    leap_cfg:      PmccLeapConfig
    short_cfg:     PmccShortConfig
    liquidity_cfg: PmccLiquidityConfig

    def select_leap(
        self,
        underlying: str,
        chain:      List[Dict[str, Any]],
        spot_price: float,
    ) -> Optional[SelectedOption]:
        candidates = self._parse_chain(underlying, chain)

        dte_ok    = [c for c in candidates
                     if self.leap_cfg.dte_min <= c.dte <= self.leap_cfg.dte_max]
        delta_ok  = [c for c in dte_ok
                     if abs(c.delta - self.leap_cfg.target_delta) < 0.15]
        ask_ok    = [c for c in delta_ok if c.ask > 0]
        liquid_ok = [c for c in ask_ok
                     if c.spread_pct is not None
                     and c.spread_pct <= self.liquidity_cfg.max_leap_spread_pct]

        if not liquid_ok:
            logger.warning(
                "No LEAP candidates | underlying=%s parsed=%d dte_ok=%d delta_ok=%d "
                "ask_ok=%d liquid_ok=%d | dte=[%d,%d] target_delta=%.2f "
                "max_spread_pct=%.2f spot=%.2f",
                underlying, len(candidates), len(dte_ok), len(delta_ok),
                len(ask_ok), len(liquid_ok),
                self.leap_cfg.dte_min, self.leap_cfg.dte_max,
                self.leap_cfg.target_delta,
                self.liquidity_cfg.max_leap_spread_pct,
                spot_price,
            )
            return None

        best = min(liquid_ok, key=lambda c: abs(c.delta - self.leap_cfg.target_delta))
        logger.info(
            "LEAP selected | underlying=%s symbol=%s dte=%d delta=%.3f "
            "ask=%.2f bid=%.2f spread_pct=%.3f strike=%.2f expiry=%s",
            underlying, best.option_symbol, best.dte, best.delta,
            best.ask, best.bid, best.spread_pct or 0.0, best.strike,
            best.contract.expiry.date(),
        )
        return SelectedOption(option_symbol=best.option_symbol, contract=best.contract)

    def select_near(
        self,
        underlying:  str,
        chain:       List[Dict[str, Any]],
        leap_strike: float,
        spot_price:  float = 0.0,
        leap_expiry: Optional[datetime] = None,  # NEAR must expire BEFORE this
    ) -> Optional[SelectedOption]:
        candidates = self._parse_chain(underlying, chain)

        dte_ok    = [c for c in candidates
                     if self.short_cfg.dte_min <= c.dte <= self.short_cfg.dte_max]
        delta_ok  = [c for c in dte_ok
                     if abs(c.delta - self.short_cfg.target_delta) < 0.10]
        strike_ok = [c for c in delta_ok if c.strike >= leap_strike]
        bid_ok    = [c for c in strike_ok if c.bid > 0]
        liquid_ok = [c for c in bid_ok
                     if c.spread_pct is not None
                     and c.spread_pct <= self.liquidity_cfg.max_near_spread_pct]

        # Safety: the NEAR must expire strictly before the LEAP.
        # If the NEAR outlives the LEAP, you'd be naked short after LEAP expiry.
        if leap_expiry is not None:
            before_leap = [c for c in liquid_ok if c.contract.expiry < leap_expiry]
            if len(before_leap) < len(liquid_ok):
                rejected = len(liquid_ok) - len(before_leap)
                logger.info(
                    "NEAR expiry filter | underlying=%s rejected=%d contracts "
                    "that expire on or after LEAP expiry=%s",
                    underlying, rejected, leap_expiry.date(),
                )
            liquid_ok = before_leap
        else:
            logger.warning(
                "select_near called without leap_expiry — "
                "cannot enforce NEAR-before-LEAP constraint | underlying=%s",
                underlying,
            )

        if not liquid_ok:
            logger.warning(
                "No NEAR candidates | underlying=%s parsed=%d dte_ok=%d delta_ok=%d "
                "strike_ok=%d bid_ok=%d liquid_ok=%d | dte=[%d,%d] target_delta=%.2f "
                "leap_strike=%.2f leap_expiry=%s spot=%.2f max_spread_pct=%.2f",
                underlying, len(candidates), len(dte_ok), len(delta_ok),
                len(strike_ok), len(bid_ok), len(liquid_ok),
                self.short_cfg.dte_min, self.short_cfg.dte_max,
                self.short_cfg.target_delta,
                leap_strike,
                leap_expiry.date() if leap_expiry else "none",
                spot_price,
                self.liquidity_cfg.max_near_spread_pct,
            )
            return None

        best = min(liquid_ok, key=lambda c: abs(c.delta - self.short_cfg.target_delta))
        logger.info(
            "NEAR selected | underlying=%s symbol=%s dte=%d delta=%.3f "
            "bid=%.2f ask=%.2f spread_pct=%.3f strike=%.2f expiry=%s leap_expiry=%s",
            underlying, best.option_symbol, best.dte, best.delta,
            best.bid, best.ask, best.spread_pct or 0.0, best.strike,
            best.contract.expiry.date(),
            leap_expiry.date() if leap_expiry else "none",
        )
        return SelectedOption(option_symbol=best.option_symbol, contract=best.contract)

    def _parse_chain(self, underlying: str, chain: List[Dict[str, Any]]) -> List[ContractCandidate]:
        candidates: List[ContractCandidate] = []
        for row in chain:
            sym = str(row.get("contract_symbol", "")).strip().upper()
            if not sym:
                continue
            try:
                parsed = parse_osi(sym)
            except ValueError:
                continue
            if parsed.right != "C":
                continue

            greeks = row.get("greeks") or {}
            quote  = row.get("latestQuote") or {}

            delta = _safe_float(greeks.get("delta"))
            ask   = _safe_float(quote.get("ap"))
            bid   = _safe_float(quote.get("bp"))
            mid   = (ask + bid) / 2.0 if ask > 0 and bid > 0 else ask or bid
            dte   = _dte(parsed.expiry)

            contract = OptionContract(
                underlying=underlying,
                expiry=parsed.expiry,
                strike=parsed.strike,
                right=OptionRight.CALL,
                option_symbol=sym,
            )
            candidates.append(ContractCandidate(
                option_symbol=sym,
                contract=contract,
                dte=dte,
                delta=delta,
                ask=ask,
                bid=bid,
                mid=mid,
            ))
        return candidates