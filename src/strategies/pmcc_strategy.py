from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from src.config.yaml_config import PmccConfig
from src.domain.intents import (
    OptionIntentPayload,
    PmccIntentPayload,
    PositionIntent,
    SingleOptionLeg,
    TradeIntent,
)

from src.domain.types import OptionChainRequest, Symbol
from src.orchestration.cycle_snapshot import CycleSnapshotABC
from src.strategies.interfaces import OptionChainConsumerABC, StrategyABC
from src.strategies.pmcc_selector import PmccContractSelector
from src.strategies.pmcc_state_machine import PmccState, PmccStateClassifier
from src.utilities.logger import setup_logger

logger = setup_logger("PmccStrategy")


@dataclass
class PmccStrategy(StrategyABC, OptionChainConsumerABC):
    config:     PmccConfig
    strategy_id: str                    = "pmcc"
    _classifier: PmccStateClassifier    = field(default_factory=PmccStateClassifier, init=False)
    _selector:   PmccContractSelector   = field(init=False)

    def __post_init__(self) -> None:
        self._selector = PmccContractSelector(
            leap_cfg=self.config.leap,
            short_cfg=self.config.short,
            liquidity_cfg=self.config.liquidity,
        )

    # ------------------------------------------------------------------
    # StrategyABC
    # ------------------------------------------------------------------

    def get_symbols(self) -> List[Symbol]:
        return [Symbol(self.config.underlying_symbol.strip().upper())]

    def generate_intents(self, snapshot: CycleSnapshotABC) -> List[TradeIntent]:
        sym      = Symbol(self.config.underlying_symbol.strip().upper())
        holdings = self._classifier.classify(
            underlying=str(sym),
            positions=list(snapshot.positions()),
            open_orders=list(snapshot.open_orders()),
        )

        logger.info("PMCC state | symbol=%s state=%s", sym, holdings.state.value)

        if holdings.state == PmccState.PENDING:
            logger.info("PMCC PENDING — skipping cycle | symbol=%s", sym)
            return []

        if holdings.state == PmccState.FLAT:
            return self._intents_entry(sym, snapshot)

        if holdings.state == PmccState.LEAP_ONLY:
            return self._intents_sell_near(sym, snapshot, holdings.leap_position)

        if holdings.state == PmccState.COVERED:
            return self._intents_manage(sym, snapshot, holdings)

        if holdings.state == PmccState.NEAR_ONLY:
            logger.warning("PMCC NEAR_ONLY — illegal state | symbol=%s", sym)
            return self._intents_close_near(sym, holdings.near_position)

        return []

    # ------------------------------------------------------------------
    # OptionChainConsumerABC
    # ------------------------------------------------------------------

    def get_option_chain_requests(self, snapshot: CycleSnapshotABC) -> List[OptionChainRequest]:
        sym       = self.config.underlying_symbol.strip().upper()
        today     = date.today()
        leap_cfg  = self.config.leap
        short_cfg = self.config.short

        return [
            OptionChainRequest(
                underlying=sym,
                request_id="leaps",
                include_calls=True,
                include_puts=False,
                expiration_date_gte=today + timedelta(days=leap_cfg.dte_min),
                expiration_date_lte=today + timedelta(days=leap_cfg.dte_max),
            ),
            OptionChainRequest(
                underlying=sym,
                request_id="shorts",
                include_calls=True,
                include_puts=False,
                expiration_date_gte=today + timedelta(days=short_cfg.dte_min),
                expiration_date_lte=today + timedelta(days=short_cfg.dte_max),
            ),
        ]

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _intents_entry(self, sym: Symbol, snapshot: CycleSnapshotABC) -> List[TradeIntent]:
        chains    = snapshot.option_chains()
        sym_str   = str(sym)
        leap_key  = _find_key(chains, sym_str, "leaps")
        short_key = _find_key(chains, sym_str, "shorts")

        if leap_key is None or short_key is None:
            missing = [k for k in ("leaps", "shorts") if _find_key(chains, sym_str, k) is None]
            available = [(str(k[0]), k[1]) for k in chains]
            logger.warning(
                "Entry chains not in snapshot | symbol=%s missing=%s available=%s",
                sym_str, missing, available,
            )
            return []

        leap_chain  = list(chains[leap_key])
        short_chain = list(chains[short_key])
        spot        = _get_spot(snapshot, sym_str)

        logger.info(
            "PMCC entry attempt | symbol=%s spot=%.2f leap_chain=%d short_chain=%d",
            sym_str, spot, len(leap_chain), len(short_chain),
        )

        leap_selection = self._selector.select_leap(sym_str, leap_chain, spot)
        if leap_selection is None:
            logger.warning(
                "No LEAP selected — no intent generated | symbol=%s spot=%.2f leap_chain=%d",
                sym_str, spot, len(leap_chain),
            )
            return []

        near_selection = self._selector.select_near(
            sym_str, short_chain, float(leap_selection.contract.strike), spot_price=spot,
        )
        if near_selection is None:
            logger.warning(
                "No NEAR selected — no intent generated | symbol=%s leap=%s leap_strike=%.2f spot=%.2f short_chain=%d",
                sym_str, leap_selection.option_symbol,
                float(leap_selection.contract.strike), spot, len(short_chain),
            )
            return []

        logger.info(
            "PMCC entry intent created | symbol=%s leap=%s near=%s spot=%.2f",
            sym_str, leap_selection.option_symbol, near_selection.option_symbol, spot,
        )

        payload = PmccIntentPayload(
            underlying_symbol=sym,
            leap_leg=leap_selection,
            near_leg=near_selection,
        )

        return [TradeIntent.create(
            strategy_id=self.strategy_id,
            symbol=sym,
            payload=payload,
        )]

    def _intents_sell_near(
        self,
        sym: Symbol,
        snapshot: CycleSnapshotABC,
        leap_position: Optional[Dict[str, Any]],
    ) -> List[TradeIntent]:
        chains    = snapshot.option_chains()
        sym_str   = str(sym)
        short_key = _find_key(chains, sym_str, "shorts")

        if short_key is None:
            logger.warning("Shorts chain not in snapshot | symbol=%s available=%s", sym_str, [(str(k[0]), k[1]) for k in chains])
            return []

        leap_strike: Optional[float] = None
        if leap_position:
            try:
                from src.risk.pmcc_sizer import parse_osi
                leap_strike = float(parse_osi(str(leap_position.get("symbol", ""))).strike)
            except Exception:
                pass

        near = self._selector.select_near(sym_str, list(chains[short_key]), leap_strike or 0, spot_price=_get_spot(snapshot, sym_str))
        if near is None:
            logger.warning("No NEAR selected for sell_near | symbol=%s leap_strike=%s", sym_str, leap_strike)
            return []

        logger.info("NEAR sell intent created | symbol=%s near=%s", sym_str, near.option_symbol)

        payload = OptionIntentPayload(
            underlying_symbol=sym,
            leg=SingleOptionLeg(
                contract_symbol=near.option_symbol,
                position_intent=PositionIntent.SELL_TO_OPEN,
                qty=1,
            ),
        )
        return [TradeIntent.create(strategy_id=self.strategy_id, symbol=sym, payload=payload)]

    def _intents_manage(
        self,
        sym: Symbol,
        snapshot: CycleSnapshotABC,
        holdings,
    ) -> List[TradeIntent]:
        near = holdings.near_position
        if near is None:
            return []

        near_sym = str(near.get("symbol", ""))
        if not near_sym:
            return []

        # Check if we should roll the near
        dte = near.get("dte") or near.get("days_to_expiry")
        if dte is not None and int(dte) <= self.config.roll.dte_threshold:
            logger.info("Rolling NEAR (DTE threshold) | symbol=%s near=%s dte=%s", sym, near_sym, dte)
            return self._intents_roll_near(sym, near_sym, snapshot)

        delta = near.get("delta")
        if delta is not None and abs(float(delta)) >= self.config.roll.delta_threshold:
            logger.info("Rolling NEAR (delta threshold) | symbol=%s near=%s delta=%s", sym, near_sym, delta)
            return self._intents_roll_near(sym, near_sym, snapshot)

        logger.info("PMCC covered, no action needed | symbol=%s near=%s dte=%s delta=%s", sym, near_sym, dte, delta)
        return []

    def _intents_roll_near(
        self,
        sym: Symbol,
        near_sym: str,
        snapshot: CycleSnapshotABC,
    ) -> List[TradeIntent]:
        # BTC existing near, then STO new near — two intents
        btc_payload = OptionIntentPayload(
            underlying_symbol=sym,
            leg=SingleOptionLeg(
                contract_symbol=near_sym,
                position_intent=PositionIntent.BUY_TO_CLOSE,
                qty=1,
            ),
        )
        btc = TradeIntent.create(
            strategy_id=self.strategy_id,
            symbol=sym,
            payload=btc_payload,
            tags=("roll_btc",),
        )

        chains    = snapshot.option_chains()
        short_key = _find_key(chains, str(sym), "shorts")
        if short_key is None:
            logger.warning("Shorts chain missing for roll | symbol=%s", sym)
            return [btc]

        new_near = self._selector.select_near(str(sym), list(chains[short_key]), 0, spot_price=_get_spot(snapshot, str(sym)))
        if new_near is None:
            logger.warning("No new NEAR for roll | symbol=%s", sym)
            return [btc]

        logger.info("Roll intents created | symbol=%s btc=%s sto=%s", sym, near_sym, new_near.option_symbol)

        sto_payload = OptionIntentPayload(
            underlying_symbol=sym,
            leg=SingleOptionLeg(
                contract_symbol=new_near.option_symbol,
                position_intent=PositionIntent.SELL_TO_OPEN,
                qty=1,
            ),
        )
        sto = TradeIntent.create(
            strategy_id=self.strategy_id,
            symbol=sym,
            payload=sto_payload,
            tags=("roll_sto",),
        )
        return [btc, sto]

    def _intents_close_near(
        self,
        sym: Symbol,
        near_position: Optional[Dict[str, Any]],
    ) -> List[TradeIntent]:
        if near_position is None:
            return []
        near_sym = str(near_position.get("symbol", ""))
        if not near_sym:
            return []
        payload = OptionIntentPayload(
            underlying_symbol=sym,
            leg=SingleOptionLeg(
                contract_symbol=near_sym,
                position_intent=PositionIntent.BUY_TO_CLOSE,
                qty=1,
            ),
        )
        return [TradeIntent.create(strategy_id=self.strategy_id, symbol=sym, payload=payload)]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _find_key(chains, sym_str: str, request_id: str):
    for k in chains:
        if str(k[0]).upper() == sym_str.upper() and k[1] == request_id:
            return k
    return None


def _get_spot(snapshot: CycleSnapshotABC, sym_str: str) -> float:
    quotes = snapshot.asset_quotes()
    for k, v in quotes.items():
        if str(k).upper() == sym_str.upper():
            return v.mid or v.ask or v.bid or 0.0
    return 0.0
