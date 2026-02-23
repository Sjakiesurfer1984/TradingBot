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

        Called on the pre-snapshot (positions + orders + account data, no chains).
        We classify state and evaluate all management conditions here using only
        data already available — OSI-derived strikes/DTE, market_value, cost_basis,
        and spot price — so we fetch chains only when there is real work to do.

        Chain requirements:
          PENDING   → nothing
          NEAR_ONLY → nothing         (BTC uses known symbol, no chain needed)
          FLAT      → leaps + shorts  (only if units < max_units AND buying power > 0)
          LEAP_ONLY → shorts only
          COVERED   → evaluated per condition, highest priority wins:
                        LEAP danger        → nothing (all closes use known symbols)
                        LEAP roll          → leaps + shorts (rebuild full spread)
                        NEAR roll trigger  → shorts only
                        no trigger         → nothing
        """
        sym       = self.config.underlying_symbol.strip().upper()
        today     = date.today()
        leap_cfg  = self.config.leap
        short_cfg = self.config.short
        roll_cfg  = self.config.roll

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

        # ------------------------------------------------------------------ #
        # States that never need chains                                        #
        # ------------------------------------------------------------------ #

        if holdings.state in (PmccState.PENDING, PmccState.NEAR_ONLY):
            logger.info(
                "Chain fetch skipped — state=%s | symbol=%s",
                holdings.state.value, sym,
            )
            return []

        # ------------------------------------------------------------------ #
        # FLAT — entry only if capacity and capital exist                      #
        # ------------------------------------------------------------------ #

        if holdings.state == PmccState.FLAT:
            buying_power = snapshot.option_buying_power()

            units_held = sum(
                abs(int(float(p.get("qty") or 0)))
                for p in snapshot.positions()
                if str(p.get("asset_class", "")).lower() == "us_option"
                and float(p.get("qty") or 0) > 0
            )

            if units_held >= self.config.max_units:
                logger.info(
                    "Chain fetch skipped — max_units reached (%d/%d) | symbol=%s",
                    units_held, self.config.max_units, sym,
                )
                return []

            if buying_power <= 0:
                logger.info(
                    "Chain fetch skipped — no buying power (%.2f) | symbol=%s",
                    buying_power, sym,
                )
                return []

            logger.info(
                "FLAT — fetching leaps + shorts | symbol=%s units=%d/%d bp=%.2f",
                sym, units_held, self.config.max_units, buying_power,
            )
            return [leap_req, short_req]

        # ------------------------------------------------------------------ #
        # LEAP_ONLY — always need to sell a NEAR                              #
        # ------------------------------------------------------------------ #

        if holdings.state == PmccState.LEAP_ONLY:
            return [short_req]

        # ------------------------------------------------------------------ #
        # COVERED — evaluate conditions in priority order.                     #
        #                                                                      #
        # Priority:                                                            #
        #   1. LEAP danger  → close everything, no chains needed               #
        #   2. LEAP roll    → leaps + shorts (full spread rebuild)             #
        #   3. NEAR roll    → shorts only                                      #
        #                                                                      #
        # Higher priority preempts lower — LEAP action and independent NEAR   #
        # rolls never fire in the same cycle.                                  #
        # ------------------------------------------------------------------ #

        if holdings.state == PmccState.COVERED:
            spot = _get_spot(snapshot, sym)

            # Priority 1: LEAP danger
            for leap in holdings.leap_positions:
                leap_strike = leap.get("derived_strike") or 0.0
                if leap_strike > 0 and spot > 0:
                    if (spot / leap_strike) <= roll_cfg.leap_strike_danger:
                        logger.info(
                            "Chain decision: LEAP danger close — spot %.2f <= %.1f%% "
                            "of strike %.2f | symbol=%s leap=%s",
                            spot, roll_cfg.leap_strike_danger * 100, leap_strike,
                            sym, leap.get("symbol"),
                        )
                        # All closes use known symbols — no chains needed.
                        return []

            # Priority 2: LEAP roll
            for leap in holdings.leap_positions:
                leap_dte = leap.get("derived_dte") or 999
                if leap_dte <= roll_cfg.leap_dte_threshold:
                    logger.info(
                        "Chain decision: LEAP roll — DTE %d <= %d | symbol=%s leap=%s",
                        leap_dte, roll_cfg.leap_dte_threshold, sym, leap.get("symbol"),
                    )
                    # Need leaps to buy a new LEAP, shorts to re-sell a new NEAR.
                    return [leap_req, short_req]

            # Priority 3: NEAR roll — only evaluated if no LEAP action needed
            for near in holdings.near_positions:
                near_dte    = near.get("derived_dte") or 999
                near_strike = near.get("derived_strike") or 0.0

                if near_dte <= roll_cfg.dte_threshold:
                    logger.info(
                        "Chain decision: NEAR roll — DTE %d <= %d | symbol=%s near=%s",
                        near_dte, roll_cfg.dte_threshold, sym, near.get("symbol"),
                    )
                    return [short_req]

                cost_basis   = float(near.get("cost_basis")   or 0.0)
                market_value = float(near.get("market_value") or 0.0)
                if cost_basis < 0:
                    premium_received = abs(cost_basis)
                    current_cost     = abs(market_value)
                    if premium_received > 0:
                        profit_captured = (premium_received - current_cost) / premium_received
                        if profit_captured >= roll_cfg.profit_pct:
                            logger.info(
                                "Chain decision: NEAR roll — profit %.1f%% >= %.1f%% "
                                "| symbol=%s near=%s",
                                profit_captured * 100, roll_cfg.profit_pct * 100,
                                sym, near.get("symbol"),
                            )
                            return [short_req]

                if near_strike > 0 and spot > 0:
                    if (spot / near_strike) >= roll_cfg.near_strike_proximity:
                        logger.info(
                            "Chain decision: NEAR roll — spot %.2f >= %.1f%% of "
                            "strike %.2f | symbol=%s near=%s",
                            spot, roll_cfg.near_strike_proximity * 100, near_strike,
                            sym, near.get("symbol"),
                        )
                        return [short_req]

            logger.info(
                "COVERED, all positions stable — chain fetch skipped | symbol=%s", sym,
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

        leap_strike: Optional[float]    = None
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
        Emit intents for an existing COVERED position.

        Priority order — only one branch fires per cycle:
          1. LEAP danger   → BTC all NEARs, STC all LEAPs
          2. LEAP roll     → BTC all NEARs → STC old LEAP → BTO new LEAP → STO new NEAR
          3. NEAR roll(s)  → BTC expiring NEAR → STO new NEAR (per near that triggers)

        LEAP conditions preempt NEAR conditions. When the LEAP is being acted on,
        NEARs are handled as part of that action — not independently.
        """
        roll_cfg = self.config.roll
        spot     = _get_spot(snapshot, str(sym))
        intents: List[TradeIntent] = []

        # ------------------------------------------------------------------ #
        # Priority 1: LEAP danger — close everything                          #
        # ------------------------------------------------------------------ #

        leap_danger = any(
            (leap.get("derived_strike") or 0.0) > 0
            and spot > 0
            and (spot / (leap.get("derived_strike") or 1.0)) <= roll_cfg.leap_strike_danger
            for leap in holdings.leap_positions
        )

        if leap_danger:
            logger.warning(
                "LEAP danger — closing entire spread | symbol=%s spot=%.2f", sym, spot,
            )
            # BTC all NEARs first — must be flat the short before closing the long.
            for near in holdings.near_positions:
                near_sym = str(near.get("symbol", ""))
                if not near_sym:
                    continue
                intents.append(TradeIntent.create(
                    strategy_id=self.strategy_id, symbol=sym,
                    payload=OptionIntentPayload(
                        underlying_symbol=sym,
                        leg=SingleOptionLeg(
                            contract_symbol=near_sym,
                            position_intent=PositionIntent.BUY_TO_CLOSE,
                            qty=abs(int(float(near.get("qty") or 1))),
                        ),
                    ),
                    tags=("danger_close_near",),
                ))
            # STC all LEAPs.
            for leap in holdings.leap_positions:
                leap_sym = str(leap.get("symbol", ""))
                if not leap_sym:
                    continue
                intents.append(TradeIntent.create(
                    strategy_id=self.strategy_id, symbol=sym,
                    payload=OptionIntentPayload(
                        underlying_symbol=sym,
                        leg=SingleOptionLeg(
                            contract_symbol=leap_sym,
                            position_intent=PositionIntent.SELL_TO_CLOSE,
                            qty=abs(int(float(leap.get("qty") or 1))),
                        ),
                    ),
                    tags=("danger_close_leap",),
                ))
            return intents

        # ------------------------------------------------------------------ #
        # Priority 2: LEAP roll                                               #
        # Sequence: BTC all NEARs → STC old LEAP → BTO new LEAP → STO new NEAR
        # ------------------------------------------------------------------ #

        leap_needs_roll = any(
            (leap.get("derived_dte") or 999) <= roll_cfg.leap_dte_threshold
            for leap in holdings.leap_positions
        )

        if leap_needs_roll:
            logger.info("LEAP roll — rebuilding entire spread | symbol=%s", sym)

            chains    = snapshot.option_chains()
            leap_key  = _find_key(chains, str(sym), "leaps")
            short_key = _find_key(chains, str(sym), "shorts")

            for leap in holdings.leap_positions:
                leap_sym = str(leap.get("symbol", ""))
                if not leap_sym:
                    continue
                qty = abs(int(float(leap.get("qty") or 1)))

                # Step 1: BTC all NEARs paired with this LEAP
                for near in holdings.near_positions:
                    near_sym = str(near.get("symbol", ""))
                    if not near_sym:
                        continue
                    intents.append(TradeIntent.create(
                        strategy_id=self.strategy_id, symbol=sym,
                        payload=OptionIntentPayload(
                            underlying_symbol=sym,
                            leg=SingleOptionLeg(
                                contract_symbol=near_sym,
                                position_intent=PositionIntent.BUY_TO_CLOSE,
                                qty=abs(int(float(near.get("qty") or 1))),
                            ),
                        ),
                        tags=("leap_roll_btc_near",),
                    ))

                # Step 2: STC the expiring LEAP
                intents.append(TradeIntent.create(
                    strategy_id=self.strategy_id, symbol=sym,
                    payload=OptionIntentPayload(
                        underlying_symbol=sym,
                        leg=SingleOptionLeg(
                            contract_symbol=leap_sym,
                            position_intent=PositionIntent.SELL_TO_CLOSE,
                            qty=qty,
                        ),
                    ),
                    tags=("leap_roll_stc",),
                ))

                # Step 3: BTO new LEAP
                if leap_key:
                    new_leap = self._selector.select_leap(
                        str(sym), list(chains[leap_key]), spot,
                    )
                    if new_leap:
                        logger.info(
                            "LEAP roll — new LEAP selected | symbol=%s new=%s",
                            sym, new_leap.option_symbol,
                        )
                        intents.append(TradeIntent.create(
                            strategy_id=self.strategy_id, symbol=sym,
                            payload=OptionIntentPayload(
                                underlying_symbol=sym,
                                leg=SingleOptionLeg(
                                    contract_symbol=new_leap.option_symbol,
                                    position_intent=PositionIntent.BUY_TO_OPEN,
                                    qty=qty,
                                ),
                            ),
                            tags=("leap_roll_bto",),
                        ))

                        # Step 4: STO new NEAR against the new LEAP
                        if short_key:
                            new_near = self._selector.select_near(
                                str(sym), list(chains[short_key]),
                                float(new_leap.contract.strike),
                                spot_price=spot,
                                leap_expiry=new_leap.contract.expiry,
                            )
                            if new_near:
                                logger.info(
                                    "LEAP roll — new NEAR selected | symbol=%s new=%s",
                                    sym, new_near.option_symbol,
                                )
                                intents.append(TradeIntent.create(
                                    strategy_id=self.strategy_id, symbol=sym,
                                    payload=OptionIntentPayload(
                                        underlying_symbol=sym,
                                        leg=SingleOptionLeg(
                                            contract_symbol=new_near.option_symbol,
                                            position_intent=PositionIntent.SELL_TO_OPEN,
                                            qty=qty,
                                        ),
                                    ),
                                    tags=("leap_roll_sto_near",),
                                ))
                            else:
                                logger.warning(
                                    "LEAP roll — no new NEAR found; will re-sell next cycle "
                                    "| symbol=%s", sym,
                                )
                        else:
                            logger.warning(
                                "LEAP roll — shorts chain missing; cannot re-sell NEAR "
                                "| symbol=%s", sym,
                            )
                    else:
                        logger.warning(
                            "LEAP roll — no new LEAP found in chain | symbol=%s", sym,
                        )
                else:
                    logger.warning(
                        "LEAP roll — leaps chain missing from snapshot | symbol=%s", sym,
                    )

            # LEAP roll owns the NEARs — do not fall through to NEAR roll logic.
            return intents

        # ------------------------------------------------------------------ #
        # Priority 3: NEAR roll(s)                                            #
        # Only reached when no LEAP action was needed this cycle.             #
        # ------------------------------------------------------------------ #

        for near in holdings.near_positions:
            near_sym    = str(near.get("symbol", ""))
            near_dte    = near.get("derived_dte") or 999
            near_strike = near.get("derived_strike") or 0.0
            if not near_sym:
                continue

            roll_near = False
            reason    = ""

            # Condition 1: DTE
            if near_dte <= roll_cfg.dte_threshold:
                roll_near = True
                reason    = f"DTE {near_dte} <= {roll_cfg.dte_threshold}"

            # Condition 2: Profit target
            # cost_basis is negative (credit received when short).
            # market_value is the current mark (also negative when short).
            if not roll_near:
                cost_basis   = float(near.get("cost_basis")   or 0.0)
                market_value = float(near.get("market_value") or 0.0)
                if cost_basis < 0:
                    premium_received = abs(cost_basis)
                    current_cost     = abs(market_value)
                    if premium_received > 0:
                        profit_captured = (premium_received - current_cost) / premium_received
                        if profit_captured >= roll_cfg.profit_pct:
                            roll_near = True
                            reason    = (
                                f"profit {profit_captured:.1%} >= {roll_cfg.profit_pct:.1%}"
                            )

            # Condition 3: Spot proximity to NEAR strike
            if not roll_near and near_strike > 0 and spot > 0:
                if (spot / near_strike) >= roll_cfg.near_strike_proximity:
                    roll_near = True
                    reason    = (
                        f"spot {spot:.2f} >= {roll_cfg.near_strike_proximity:.1%} "
                        f"of strike {near_strike:.2f}"
                    )

            if not roll_near:
                logger.info(
                    "NEAR stable | symbol=%s near=%s dte=%d", sym, near_sym, near_dte,
                )
                continue

            logger.info(
                "Rolling NEAR | symbol=%s near=%s reason=%s", sym, near_sym, reason,
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
            intents.extend(
                self._intents_roll_near(sym, near_sym, snapshot, leap_expiry)
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