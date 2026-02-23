from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    config:      PmccConfig
    strategy_id: str = "pmcc"

    _classifier: PmccStateClassifier  = field(init=False)
    _selector:   PmccContractSelector = field(init=False)

    def __post_init__(self) -> None:
        self._classifier = PmccStateClassifier(
            leap_dte_min=self.config.leap.dte_min,
            near_dte_max=self.config.short.dte_max,
        )
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

        logger.info(
            "PMCC state | symbol=%s state=%s long_contracts=%d "
            "short_contracts=%d uncovered=%d",
            sym, holdings.state.value,
            holdings.long_contracts, holdings.short_contracts, holdings.uncovered,
        )

        if holdings.state == PmccState.PENDING:
            logger.info("PMCC PENDING — skipping cycle | symbol=%s", sym)
            return []

        if holdings.state == PmccState.FLAT:
            return self._intents_entry(sym, snapshot)

        if holdings.state == PmccState.LEAP_ONLY:
            # uncovered = number of LEAP contracts with no matching short NEAR.
            # We sell one NEAR per uncovered contract.
            # All uncovered contracts share the same LEAP position object (qty=N),
            # so we use the same leap_position for strike reference each time.
            logger.info(
                "PMCC LEAP_ONLY — selling %d NEAR(s) | symbol=%s",
                holdings.uncovered, sym,
            )
            intents = []
            for _ in range(holdings.uncovered):
                intents.extend(
                    self._intents_sell_near(sym, snapshot, holdings.leap_position)
                )
            return intents

        if holdings.state == PmccState.COVERED:
            return self._intents_manage(sym, snapshot, holdings)

        if holdings.state == PmccState.NEAR_ONLY:
            logger.warning("PMCC NEAR_ONLY — illegal state, closing NEAR | symbol=%s", sym)
            return self._intents_close_near(sym, holdings.near_positions[0])

        return []

    # ------------------------------------------------------------------
    # OptionChainConsumerABC — called during the PRE-snapshot phase
    # ------------------------------------------------------------------

    def get_option_chain_requests(self, snapshot: CycleSnapshotABC) -> List[OptionChainRequest]:
        """
        Declare which option chains we need BEFORE the full snapshot is built.

        The orchestrator calls this on the pre-snapshot (positions + orders,
        no chains). We classify state here so we only fetch what we actually
        need — skipping the ~4s chain fetch entirely when covered and stable.

        Chain requirements by state:
          FLAT      → leaps + shorts  (need both legs for a new entry)
          LEAP_ONLY → shorts only     (have the LEAP, need to sell a NEAR)
          COVERED   → shorts only IF a near is within roll DTE, else nothing
          NEAR_ONLY → nothing         (close the near at market, no chain needed)
          PENDING   → nothing         (order in-flight, skip everything)
        """
        sym       = self.config.underlying_symbol.strip().upper()
        today     = date.today()
        leap_cfg  = self.config.leap
        short_cfg = self.config.short

        holdings = self._classifier.classify(
            underlying=sym,
            positions=list(snapshot.positions()),
            open_orders=list(snapshot.open_orders()),
        )

        leap_req = OptionChainRequest(
            underlying=sym,
            request_id="leaps",
            include_calls=True,
            include_puts=False,
            expiration_date_gte=today + timedelta(days=leap_cfg.dte_min),
            expiration_date_lte=today + timedelta(days=leap_cfg.dte_max),
        )
        short_req = OptionChainRequest(
            underlying=sym,
            request_id="shorts",
            include_calls=True,
            include_puts=False,
            expiration_date_gte=today + timedelta(days=short_cfg.dte_min),
            expiration_date_lte=today + timedelta(days=short_cfg.dte_max),
        )

        if holdings.state in (PmccState.PENDING, PmccState.NEAR_ONLY):
            logger.info(
                "Chain fetch skipped — state=%s needs no chains | symbol=%s",
                holdings.state.value, sym,
            )
            return []

        if holdings.state == PmccState.FLAT:
            return [leap_req, short_req]

        if holdings.state == PmccState.LEAP_ONLY:
            return [short_req]

        if holdings.state == PmccState.COVERED:
            roll_needed = any(
                (p.get("derived_dte") or 999) <= self.config.roll.dte_threshold
                for p in holdings.near_positions
            )
            if roll_needed:
                logger.info(
                    "COVERED but near(s) within roll threshold — "
                    "fetching shorts chain | symbol=%s", sym,
                )
                return [short_req]
            logger.info(
                "COVERED, no roll needed — chain fetch skipped | symbol=%s", sym,
            )
            return []

        return []

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _intents_entry(self, sym: Symbol, snapshot: CycleSnapshotABC) -> List[TradeIntent]:
        chains    = snapshot.option_chains()
        sym_str   = str(sym)
        leap_key  = _find_key(chains, sym_str, "leaps")
        short_key = _find_key(chains, sym_str, "shorts")

        if leap_key is None or short_key is None:
            missing   = [k for k in ("leaps", "shorts") if _find_key(chains, sym_str, k) is None]
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
            logger.warning("No LEAP selected | symbol=%s", sym_str)
            return []

        near_selection = self._selector.select_near(
            sym_str, short_chain,
            float(leap_selection.contract.strike),
            spot_price=spot,
            leap_expiry=leap_selection.contract.expiry,
        )
        if near_selection is None:
            logger.warning(
                "No NEAR selected | symbol=%s leap=%s leap_strike=%.2f",
                sym_str, leap_selection.option_symbol,
                float(leap_selection.contract.strike),
            )
            return []

        logger.info(
            "PMCC entry intent | symbol=%s leap=%s near=%s spot=%.2f",
            sym_str, leap_selection.option_symbol, near_selection.option_symbol, spot,
        )

        payload = PmccIntentPayload(
            underlying_symbol=sym,
            leap_leg=leap_selection,
            near_leg=near_selection,
        )
        return [TradeIntent.create(strategy_id=self.strategy_id, symbol=sym, payload=payload)]

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
            logger.warning(
                "Shorts chain not in snapshot | symbol=%s available=%s",
                sym_str, [(str(k[0]), k[1]) for k in chains],
            )
            return []

        leap_strike: Optional[float] = None
        leap_expiry: Optional[datetime] = None
        if leap_position:
            try:
                from src.risk.pmcc_sizer import parse_osi
                parsed_leap = parse_osi(str(leap_position.get("symbol", "")))
                leap_strike = float(parsed_leap.strike)
                leap_expiry = parsed_leap.expiry
            except Exception:
                pass

        near = self._selector.select_near(
            sym_str, list(chains[short_key]),
            leap_strike or 0,
            spot_price=_get_spot(snapshot, sym_str),
            leap_expiry=leap_expiry,
        )
        if near is None:
            logger.warning(
                "No NEAR selected for sell_near | symbol=%s leap_strike=%s",
                sym_str, leap_strike,
            )
            return []

        logger.info("NEAR sell intent | symbol=%s near=%s", sym_str, near.option_symbol)

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
        """
        Check each NEAR for roll triggers.

        derived_dte is set by _enrich_position in the classifier — it is
        derived from the OSI symbol and is the single source of truth for DTE.
        Alpaca does not return a dte field on position objects.

        Delta-based rolling would require matching the position to a live
        chain row. That requires chain data. We only fetch the shorts chain
        when derived_dte indicates a roll is needed (see get_option_chain_requests).
        Once fetched, we could match by symbol to get current delta — not yet
        implemented; for now we roll on DTE only.
        """
        intents = []
        for near in holdings.near_positions:
            near_sym = str(near.get("symbol", ""))
            if not near_sym:
                continue

            dte = near.get("derived_dte")

            if dte is not None and dte <= self.config.roll.dte_threshold:
                logger.info(
                    "Rolling NEAR — DTE %d <= threshold %d | symbol=%s near=%s",
                    dte, self.config.roll.dte_threshold, sym, near_sym,
                )
                leap_expiry: Optional[datetime] = None
                if holdings.leap_position:
                    try:
                        from src.risk.pmcc_sizer import parse_osi
                        leap_expiry = parse_osi(
                            str(holdings.leap_position.get("symbol", ""))
                        ).expiry
                    except Exception:
                        pass
                intents.extend(self._intents_roll_near(sym, near_sym, snapshot, leap_expiry))
            else:
                logger.info(
                    "NEAR stable — no roll needed | symbol=%s near=%s dte=%s threshold=%d",
                    sym, near_sym, dte, self.config.roll.dte_threshold,
                )
        return intents

    def _intents_roll_near(
        self,
        sym: Symbol,
        near_sym: str,
        snapshot: CycleSnapshotABC,
        leap_expiry: Optional[datetime] = None,
    ) -> List[TradeIntent]:
        btc = TradeIntent.create(
            strategy_id=self.strategy_id, symbol=sym,
            payload=OptionIntentPayload(
                underlying_symbol=sym,
                leg=SingleOptionLeg(
                    contract_symbol=near_sym,
                    position_intent=PositionIntent.BUY_TO_CLOSE,
                    qty=1,
                ),
            ),
            tags=("roll_btc",),
        )

        chains    = snapshot.option_chains()
        short_key = _find_key(chains, str(sym), "shorts")
        if short_key is None:
            logger.warning("Shorts chain missing for roll | symbol=%s", sym)
            return [btc]

        new_near = self._selector.select_near(
            str(sym), list(chains[short_key]), 0,
            spot_price=_get_spot(snapshot, str(sym)),
            leap_expiry=leap_expiry,
        )
        if new_near is None:
            logger.warning("No new NEAR for roll | symbol=%s", sym)
            return [btc]

        logger.info("Roll | symbol=%s btc=%s sto=%s", sym, near_sym, new_near.option_symbol)

        sto = TradeIntent.create(
            strategy_id=self.strategy_id, symbol=sym,
            payload=OptionIntentPayload(
                underlying_symbol=sym,
                leg=SingleOptionLeg(
                    contract_symbol=new_near.option_symbol,
                    position_intent=PositionIntent.SELL_TO_OPEN,
                    qty=1,
                ),
            ),
            tags=("roll_sto",),
        )
        return [btc, sto]

    def _intents_close_near(
        self,
        sym: Symbol,
        near_position: Dict[str, Any],
    ) -> List[TradeIntent]:
        near_sym = str(near_position.get("symbol", ""))
        if not near_sym:
            return []
        return [TradeIntent.create(
            strategy_id=self.strategy_id, symbol=sym,
            payload=OptionIntentPayload(
                underlying_symbol=sym,
                leg=SingleOptionLeg(
                    contract_symbol=near_sym,
                    position_intent=PositionIntent.BUY_TO_CLOSE,
                    qty=1,
                ),
            ),
        )]


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def _find_key(chains, sym_str: str, request_id: str):
    for k in chains:
        if str(k[0]).upper() == sym_str.upper() and k[1] == request_id:
            return k
    return None


def _get_spot(snapshot: CycleSnapshotABC, sym_str: str) -> float:
    for k, v in snapshot.asset_quotes().items():
        if str(k).upper() == sym_str.upper():
            return v.mid or v.ask or v.bid or 0.0
    return 0.0