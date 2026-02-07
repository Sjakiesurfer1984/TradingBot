# src/TradingBot/v2/cycle_plan.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple, Any, Mapping

from TradingBot.domain.types import Symbol, normalise_symbol
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope
from collections.abc import Callable

PmccUnitCounter = Callable[[Symbol, Sequence[Mapping[str, Any]]], int]
logger = setup_logger("CyclePlan")


@dataclass(frozen=True)
class UnderlyingPlan:
    """
    Per-underlying plan for what expensive data to fetch in this cycle.
    """

    underlying: Symbol
    should_fetch_quote: bool
    should_fetch_option_chain: bool
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class CyclePlan:
    """
    Plan produced at the start of a cycle that decides what to fetch.

    The intent is to avoid expensive option-chain fetches when we already
    know we cannot or should not place new trades for an underlying.
    """

    underlyings: Tuple[UnderlyingPlan, ...]


def _has_open_order_for_underlying(*, open_orders: Sequence[object], underlying: Symbol) -> bool:
    """
    Returns True if any open order appears to belong to the given underlying.

    We intentionally use a permissive match:
    - If the order has a 'symbol' attribute (common for equity orders), compare that.
    - If the order has a 'client_order_id' attribute and we use the prefix scheme
      PMCC:<UNDERLYING>:<ts>, match by prefix.
    """
    u: str = str(underlying)

    for order in open_orders:
        sym: Optional[str] = getattr(order, "symbol", None)
        if sym is not None and normalise_symbol(str(sym)) == underlying:
            return True

        coid: Optional[str] = getattr(order, "client_order_id", None)
        if coid is not None:
            # Your system uses e.g. "PMCC:SPY:20260118T101523Z"
            if str(coid).startswith(f"PMCC:{u}:"):
                return True

    return False

def build_cycle_plan(
    *,
    strategy_underlyings: Iterable[Symbol],
    open_orders: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    max_pmcc_units_per_underlying: int,
    pmcc_unit_counter: PmccUnitCounter,
    skip_chain_if_open_order_exists: bool = True,
    skip_chain_if_at_unit_cap: bool = True,
) -> CyclePlan:
    """
    Build a CyclePlan for the given strategy underlyings.

    Parameters
    ----------
    strategy_underlyings:
        Underlyings that strategies might trade this cycle (already normalised if possible).
    open_orders:
        Broker open orders snapshot from RiskContext.
    positions:
        Broker positions snapshot from RiskContext.
    max_pmcc_units_per_underlying:
        Hard cap per underlying.
    pmcc_unit_counter:
        Function that returns the number of PMCC units for (positions, underlying).
        Signature expected: (positions: Sequence[object], underlying: Symbol) -> int
        This is injected so CyclePlan stays pure and testable without importing your risk code.
    skip_chain_if_open_order_exists:
        When True, avoid option-chain fetch if we have a pending order for this underlying.
    skip_chain_if_at_unit_cap:
        When True, avoid option-chain fetch if we are already at the PMCC unit cap.

    Returns
    -------
    CyclePlan
        Per-underlying plan with booleans and reasons.
    """
    with log_scope(
        "cycle_plan.build",
        logger,
        extra=f"underlyings={len(list(strategy_underlyings))} open_orders={len(open_orders)} positions={len(positions)}",
    ):
        # Normalise and dedupe underlyings while preserving a stable order
        seen: Set[str] = set()
        ordered: List[Symbol] = []
        for raw in strategy_underlyings:
            sym: Symbol = normalise_symbol(str(raw))
            if str(sym) in seen:
                continue
            seen.add(str(sym))
            ordered.append(sym)

        plans: List[UnderlyingPlan] = []

        for u in ordered:
            reasons: List[str] = []

            has_open: bool = _has_open_order_for_underlying(open_orders=open_orders, underlying=u)
            units: int = int(pmcc_unit_counter(positions, u))

            # Default to fetch quote (cheap and useful)
            should_fetch_quote: bool = True

            # Default: fetch chain, then apply skip rules
            should_fetch_chain: bool = True

            if skip_chain_if_open_order_exists and has_open:
                should_fetch_chain = False
                reasons.append("skip_option_chain: open order exists for underlying")

            if skip_chain_if_at_unit_cap and units >= int(max_pmcc_units_per_underlying):
                should_fetch_chain = False
                reasons.append("skip_option_chain: at or above PMCC unit cap")

            if should_fetch_chain:
                reasons.append("fetch_option_chain: eligible for candidate selection this cycle")

            plans.append(
                UnderlyingPlan(
                    underlying=u,
                    should_fetch_quote=should_fetch_quote,
                    should_fetch_option_chain=should_fetch_chain,
                    reasons=tuple(reasons),
                )
            )

            logger.info(
                "CyclePlan | underlying=%s fetch_quote=%s fetch_chain=%s reasons=%s",
                str(u),
                str(should_fetch_quote),
                str(should_fetch_chain),
                "; ".join(reasons) if reasons else "<none>",
            )

        return CyclePlan(underlyings=tuple(plans))
