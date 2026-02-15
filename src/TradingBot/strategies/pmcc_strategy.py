from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional, Sequence, Set

from TradingBot.config.strategy_config import PmccConfig

from TradingBot.domain.intents import TradeIntent
from TradingBot.domain.types import (
    OptionChainRequest,
    StrategyId,
    Symbol,
    normalise_symbol,
)
from TradingBot.domain.orders import TimeInForce

from TradingBot.orchestration.cycle_snapshot import CycleSnapshotABC

from TradingBot.strategies.option_chain_consumer import OptionChainConsumerABC
from TradingBot.strategies.strategy_interface import StrategyABC
from TradingBot.strategies.pmcc_state_classifier import PmccStateClassifier, PmccState, PmccHoldings
from TradingBot.strategies.pmcc_selector import PmccContractSelector, Candidate
from TradingBot.strategies.pmcc_intents import PmccIntentFactory


from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope

logger = setup_logger("PMCC Strategy")


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

    def generate_intents(self, ctx: CycleSnapshotABC) -> List[TradeIntent]:
        cfg: PmccConfig = self.config
        underlying_str: str = str(cfg.underlying_symbol).strip().upper()
        underlying: Symbol = normalise_symbol(underlying_str)

        selector = PmccContractSelector(cfg=cfg)
        classifier = PmccStateClassifier()
        factory = PmccIntentFactory(strategy_id=self.strategy_id)

        as_of_utc: datetime = ctx.as_of_utc()
        chains = ctx.option_chains()
        leap_chain: List[Dict[str, Any]] = chains.get((underlying, "pmcc_leap"), [])
        near_chain: List[Dict[str, Any]] = chains.get((underlying, "pmcc_near"), [])

        held = classifier.classify(
            underlying=underlying,
            positions=ctx.positions(),
            leap_chain=leap_chain,
            near_chain=near_chain,
        )

        if held.state == PmccState.NEAR_ONLY and held.held_near_symbol is not None:
            return [
                factory.buyback_near(
                    underlying=underlying,
                    contract_symbol=held.held_near_symbol,
                    tif=TimeInForce.DAY,
                    qty=int(held.held_near_qty_abs),
                )
            ]

        if held.state == PmccState.LEAP_ONLY:
            selected_near: Optional[str] = selector.select_near_any(chain=near_chain)
            if selected_near is None:
                return []
            return [
                factory.sell_near(
                    underlying=underlying,
                    contract_symbol=selected_near,
                    tif=TimeInForce.DAY,
                    qty=None,
                )
            ]

        if held.state == PmccState.COVERED:
            if held.held_near_symbol is None:
                return []
            if self._should_roll_near(
                held_near_symbol=str(held.held_near_symbol),
                near_chain=near_chain,
                as_of_utc=as_of_utc,
            ):
                new_near: Optional[str] = selector.select_near_any(chain=near_chain)
                if new_near is None or new_near == str(held.held_near_symbol).strip().upper():
                    return []
                return [
                    factory.roll_near(
                        underlying=underlying,
                        held_near_symbol=str(held.held_near_symbol),
                        new_near_symbol=str(new_near),
                        tif=TimeInForce.DAY,
                        qty=int(held.held_near_qty_abs),
                    )
                ]
            return []

        leap = selector.select_leg(
            chain=leap_chain,
            leg_name="LEAP",
            target_delta=float(cfg.leap.target_delta),
            dte_min=int(cfg.leap.dte_min),
            dte_max=int(cfg.leap.dte_max),
            as_of_utc=as_of_utc,
        )
        if leap is None:
            return []

        near = selector.select_leg(
            chain=near_chain,
            leg_name="NEAR",
            target_delta=float(cfg.short.target_delta),
            dte_min=int(cfg.short.dte_min),
            dte_max=int(cfg.short.dte_max),
            as_of_utc=as_of_utc,
        )
        if near is None:
            return []

        return [
            factory.entry_multileg(
                underlying=underlying,
                leap=leap,
                near=near,
                as_of_utc=as_of_utc,
                tif=TimeInForce.DAY,
            )
        ]


    def _entry_allowed(self, ctx: CycleSnapshotABC, cfg: PmccConfig, underlying: Symbol) -> bool:
        """
        Minimal timing hook.
        Uses signals if present, otherwise allows entry.
        """
        signals = ctx.signals()
        scalar = signals.scalar_signals

        leap_spread_key: str = f"{str(underlying)}.leap_spread_pct_avg"
        leap_spread_avg: Optional[float] = scalar.get(leap_spread_key)

        if leap_spread_avg is None:
            return True

        return float(leap_spread_avg) <= float(cfg.liquidity.max_leap_spread_pct)

    def _sell_near_allowed(self, ctx: CycleSnapshotABC, cfg: PmccConfig, underlying: Symbol) -> bool:
        """
        Minimal timing hook.
        Uses signals if present, otherwise allows selling NEAR.
        """
        signals = ctx.signals()
        scalar = signals.scalar_signals

        near_spread_key: str = f"{str(underlying)}.near_spread_pct_avg"
        near_spread_avg: Optional[float] = scalar.get(near_spread_key)

        if near_spread_avg is None:
            return True

        return float(near_spread_avg) <= float(cfg.liquidity.max_near_spread_pct)

    def _handle_flat(
        self,
        *,
        ctx: CycleSnapshotABC,
        cfg: PmccConfig,
        underlying: Symbol,
        as_of_utc: datetime,
        leap_chain: List[Dict[str, Any]],
        near_chain: List[Dict[str, Any]],
        selector: PmccContractSelector,
        factory: PmccIntentFactory,
    ) -> List[TradeIntent]:
        if not self._entry_allowed(ctx, cfg, underlying):
            logger.info("PMCC entry blocked by signals | underlying=%s", str(underlying))
            return []

        leap_cand: Optional[Candidate] = selector.select_by_delta(
            as_of_utc=as_of_utc,
            underlying=underlying,
            chain=leap_chain,
            target_delta=float(cfg.leap.target_delta),
            dte_min=int(cfg.leap.dte_min),
            dte_max=int(cfg.leap.dte_max),
        )
        if leap_cand is None:
            logger.warning("PMCC entry skipped | no LEAP candidate | underlying=%s", str(underlying))
            return []

        near_cand: Optional[Candidate] = selector.select_by_delta(
            as_of_utc=as_of_utc,
            underlying=underlying,
            chain=near_chain,
            target_delta=float(cfg.short.target_delta),
            dte_min=int(cfg.short.dte_min),
            dte_max=int(cfg.short.dte_max),
        )
        if near_cand is None:
            logger.warning("PMCC entry skipped | no NEAR candidate | underlying=%s", str(underlying))
            return []

        leap_sel = selector.to_selected_option(leap_cand)
        near_sel = selector.to_selected_option(near_cand)

        intent = factory.make_entry_multileg(
            underlying=underlying,
            as_of_utc=as_of_utc,
            leap=leap_sel,
            near=near_sel,
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

    def _handle_leap_only(
        self,
        *,
        ctx: CycleSnapshotABC,
        cfg: PmccConfig,
        underlying: Symbol,
        holdings: PmccHoldings,
        near_chain: List[Dict[str, Any]],
        selector: PmccContractSelector,
        factory: PmccIntentFactory,
    ) -> List[TradeIntent]:
        # LEAP-only: sell a NEAR
        if not self._sell_near_allowed(ctx, cfg, underlying):
            logger.info("PMCC sell NEAR blocked by signals | underlying=%s", str(underlying))
            return []

        selected_near: Optional[str] = selector.select_any_contract_symbol(chain=near_chain)
        if selected_near is None:
            logger.warning("PMCC manage skipped | no NEAR contract available | underlying=%s", str(underlying))
            return []

        return [
            factory.make_sell_near(
                underlying=underlying,
                contract_symbol=selected_near,
                qty=None,
                time_in_force=TimeInForce.DAY,
                tags=("pmcc", "manage", "sell_near"),
            )
        ]

    def _handle_near_only(
        self,
        *,
        cfg: PmccConfig,
        underlying: Symbol,
        holdings: PmccHoldings,
        factory: PmccIntentFactory,
    ) -> List[TradeIntent]:
        # Broken: buy back NEAR
        if holdings.held_near is None:
            return []

        logger.warning(
            "PMCC illegal state detected | underlying=%s state=%s action=%s",
            str(underlying),
            holdings.state.value,
            "BUY_TO_CLOSE_NEAR",
        )

        return [
            factory.make_buyback_near(
                underlying=underlying,
                contract_symbol=str(holdings.held_near),
                qty=None,
                time_in_force=TimeInForce.DAY,
                tags=("pmcc", "manage", "buyback_near"),
            )
        ]

    def _handle_pmcc_open(
        self,
        *,
        ctx: CycleSnapshotABC,
        cfg: PmccConfig,
        underlying: Symbol,
        holdings: PmccHoldings,
        as_of_utc: datetime,
        near_chain: List[Dict[str, Any]],
        selector: PmccContractSelector,
        factory: PmccIntentFactory,
    ) -> List[TradeIntent]:
        # Covered: decide roll using cfg.roll thresholds (next step expands this)
        if holdings.held_near is None:
            return []

        if self._should_roll_near(
            held_near_symbol=str(holdings.held_near),
            near_chain=near_chain,
            as_of_utc=as_of_utc,
        ):
            logger.info(
                "PMCC roll triggered | underlying=%s held_near=%s",
                str(underlying),
                str(holdings.held_near),
            )

            # Not implemented yet:
            # - select replacement NEAR
            # - generate roll intent (BTC old near + STO new near)
            return []

        return []

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

            dte_opt: Optional[int] = None
            try:
                suffix: str = sym[-15:]
                yymmdd: str = suffix[0:6]
                cp: str = suffix[6:7]
                if cp == "C":
                    expiry_utc: datetime = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=as_of_utc.tzinfo)
                    dte_opt = int((expiry_utc - as_of_utc).total_seconds() // 86400)
            except Exception:
                dte_opt = None

            if dte_opt is not None and int(dte_opt) <= int(roll.dte_threshold):
                return True

            greeks_any: Any = row.get("greeks")
            if isinstance(greeks_any, dict):
                try:
                    delta = float(greeks_any.get("delta"))
                except Exception:
                    delta = None

                if delta is not None and abs(float(delta)) >= float(roll.delta_threshold):
                    return True

            # Profit trigger needs entry credit/price tracking; not implemented yet.
            return False

        return False
