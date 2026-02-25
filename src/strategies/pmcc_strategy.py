from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from src.config.yaml_config import PmccConfig
from src.domain.intents import (
    CloseLegPayload,
    CloseSpreadPayload,
    EnterPmccPayload,
    PositionIntent,
    RollNearPayload,
    SelectedOption,
    TradeIntent,
)
from src.domain.types import OptionChainRequest, Symbol
from src.orchestration.cycle_snapshot import CycleSnapshot
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

    def generate_intents(self, snapshot: CycleSnapshot) -> List[TradeIntent]:
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
                intents.extend(self._intents_sell_near(sym, snapshot, holdings.leap_position))
            return intents

        if holdings.state == PmccState.COVERED:
            return self._intents_manage(sym, snapshot, holdings)

        if holdings.state == PmccState.NEAR_ONLY:
            logger.warning("PMCC NEAR_ONLY — illegal state, closing NEAR | symbol=%s", sym)
            return self._intents_close_near(sym, holdings.near_positions[0])

        return []

    # ------------------------------------------------------------------
    # OptionChainConsumerABC
    # ------------------------------------------------------------------

    def get_option_chain_requests(self, snapshot: CycleSnapshot) -> List[OptionChainRequest]:
        sym       = self.config.underlying_symbol.strip().upper()
        today     = snapshot.as_of_utc.date()
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

        if holdings.state in (PmccState.PENDING, PmccState.NEAR_ONLY):
            logger.info("Chain fetch skipped — state=%s | symbol=%s", holdings.state.value, sym)
            return []

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

        if holdings.state == PmccState.LEAP_ONLY:
            return [short_req]

        if holdings.state == PmccState.COVERED:
            spot = _get_spot(snapshot, sym)

            # Priority 1: LEAP danger — all closes use known symbols
            for leap in holdings.leap_positions:
                leap_strike = leap.get("derived_strike") or 0.0
                if leap_strike > 0 and spot > 0:
                    if (spot / leap_strike) <= roll_cfg.leap_strike_danger:
                        return []

            # Priority 2: LEAP roll
            for leap in holdings.leap_positions:
                if (leap.get("derived_dte") or 999) <= roll_cfg.leap_dte_threshold:
                    return [leap_req, short_req]

            # Priority 3: NEAR roll
            for near in holdings.near_positions:
                near_dte    = near.get("derived_dte") or 999
                near_strike = near.get("derived_strike") or 0.0

                if near_dte <= roll_cfg.near_dte_threshold:
                    return [short_req]

                cost_basis   = float(near.get("cost_basis")   or 0.0)
                market_value = float(near.get("market_value") or 0.0)
                if cost_basis < 0:
                    premium_received = abs(cost_basis)
                    current_cost     = abs(market_value)
                    if premium_received > 0:
                        profit_captured = (premium_received - current_cost) / premium_received
                        if profit_captured >= roll_cfg.near_profit_pct:
                            return [short_req]

                if near_strike > 0 and spot > 0:
                    if (spot / near_strike) >= roll_cfg.near_strike_proximity:
                        return [short_req]

            logger.info("COVERED, all positions stable — chain fetch skipped | symbol=%s", sym)
            return []

        return []

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _intents_entry(self, sym: Symbol, snapshot: CycleSnapshot) -> List[TradeIntent]:
        chains    = snapshot.option_chains
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

        spot = _get_spot(snapshot, sym_str)
        logger.info(
            "PMCC entry attempt | symbol=%s spot=%.2f leap_chain=%d short_chain=%d",
            sym_str, spot, len(chains[leap_key]), len(chains[short_key]),
        )

        leap_sel = self._selector.select_leap(sym_str, list(chains[leap_key]), spot)
        if leap_sel is None:
            logger.warning("No LEAP selected | symbol=%s", sym_str)
            return []

        near_sel = self._selector.select_near(
            sym_str, list(chains[short_key]),
            float(leap_sel.contract.strike),
            spot_price=spot,
            leap_expiry=leap_sel.contract.expiry,
        )
        if near_sel is None:
            logger.warning(
                "No NEAR selected | symbol=%s leap=%s leap_strike=%.2f",
                sym_str, leap_sel.option_symbol, float(leap_sel.contract.strike),
            )
            return []

        logger.info(
            "PMCC entry intent CREATED | symbol=%s leap=%s near=%s spot=%.2f",
            sym_str, leap_sel.option_symbol, near_sel.option_symbol, spot,
        )
        payload = EnterPmccPayload(
            underlying_symbol=sym,
            leap=leap_sel,
            near=near_sel,
            max_debit=self.config.risk.max_debit_per_spread_usd / 100,
        )
        return [TradeIntent.create(strategy_id=self.strategy_id, symbol=sym, payload=payload)]

    def _intents_sell_near(
        self,
        sym: Symbol,
        snapshot: CycleSnapshot,
        leap_position: Optional[Dict[str, Any]],
    ) -> List[TradeIntent]:
        chains    = snapshot.option_chains
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

        near_sel = self._selector.select_near(
            sym_str, list(chains[short_key]),
            leap_strike or 0,
            spot_price=_get_spot(snapshot, sym_str),
            leap_expiry=leap_expiry,
        )
        if near_sel is None:
            logger.warning(
                "No NEAR selected for sell_near | symbol=%s leap_strike=%s",
                sym_str, leap_strike,
            )
            return []

        logger.info("NEAR sell intent | symbol=%s near=%s", sym_str, near_sel.option_symbol)
        payload = CloseLegPayload(
            underlying_symbol=sym,
            contract=near_sel,
            position_intent=PositionIntent.SELL_TO_OPEN,
            qty=1,
        )
        return [TradeIntent.create(strategy_id=self.strategy_id, symbol=sym, payload=payload)]

    def _intents_manage(
        self,
        sym: Symbol,
        snapshot: CycleSnapshot,
        holdings,
    ) -> List[TradeIntent]:
        roll_cfg = self.config.roll
        spot     = _get_spot(snapshot, str(sym))

        # Priority 1: LEAP danger — close entire spread atomically
        leap_danger = any(
            (leap.get("derived_strike") or 0.0) > 0
            and spot > 0
            and (spot / (leap.get("derived_strike") or 1.0)) <= roll_cfg.leap_strike_danger
            for leap in holdings.leap_positions
        )
        if leap_danger:
            logger.warning("LEAP danger — closing entire spread | symbol=%s spot=%.2f", sym, spot)
            intents = []
            for near in holdings.near_positions:
                near_sym = str(near.get("symbol", ""))
                if not near_sym:
                    continue
                for leap in holdings.leap_positions:
                    leap_sym = str(leap.get("symbol", ""))
                    if not leap_sym:
                        continue
                    intents.append(TradeIntent.create(
                        strategy_id=self.strategy_id,
                        symbol=sym,
                        payload=CloseSpreadPayload(
                            underlying_symbol=sym,
                            near=SelectedOption(
                                option_symbol=near_sym,
                                contract=near["_contract"],
                            ) if "_contract" in near else _make_dummy_selected(near_sym),
                            leap=SelectedOption(
                                option_symbol=leap_sym,
                                contract=leap["_contract"],
                            ) if "_contract" in leap else _make_dummy_selected(leap_sym),
                        ),
                        tags=("danger_close",),
                    ))
            return intents

        # Priority 2: LEAP roll
        leap_needs_roll = any(
            (leap.get("derived_dte") or 999) <= roll_cfg.leap_dte_threshold
            for leap in holdings.leap_positions
        )
        if leap_needs_roll:
            return self._intents_leap_roll(sym, snapshot, holdings, spot)

        # Priority 3: NEAR roll(s)
        return self._intents_near_rolls(sym, snapshot, holdings, spot)

    def _intents_leap_roll(
        self,
        sym: Symbol,
        snapshot: CycleSnapshot,
        holdings,
        spot: float,
    ) -> List[TradeIntent]:
        logger.info("LEAP roll — rebuilding entire spread | symbol=%s", sym)
        chains    = snapshot.option_chains
        leap_key  = _find_key(chains, str(sym), "leaps")
        short_key = _find_key(chains, str(sym), "shorts")
        intents: List[TradeIntent] = []

        for leap in holdings.leap_positions:
            leap_sym = str(leap.get("symbol", ""))
            if not leap_sym:
                continue
            qty = abs(int(float(leap.get("qty") or 1)))

            # Close all NEARs + the expiring LEAP
            for near in holdings.near_positions:
                near_sym = str(near.get("symbol", ""))
                if not near_sym:
                    continue
                intents.append(TradeIntent.create(
                    strategy_id=self.strategy_id, symbol=sym,
                    payload=CloseLegPayload(
                        underlying_symbol=sym,
                        contract=_make_dummy_selected(near_sym),
                        position_intent=PositionIntent.BUY_TO_CLOSE,
                        qty=abs(int(float(near.get("qty") or 1))),
                    ),
                    tags=("leap_roll_btc_near",),
                ))

            intents.append(TradeIntent.create(
                strategy_id=self.strategy_id, symbol=sym,
                payload=CloseLegPayload(
                    underlying_symbol=sym,
                    contract=_make_dummy_selected(leap_sym),
                    position_intent=PositionIntent.SELL_TO_CLOSE,
                    qty=qty,
                ),
                tags=("leap_roll_stc",),
            ))

            # Open new LEAP
            if leap_key:
                new_leap = self._selector.select_leap(str(sym), list(chains[leap_key]), spot)
                if new_leap:
                    logger.info("LEAP roll — new LEAP | symbol=%s new=%s", sym, new_leap.option_symbol)
                    intents.append(TradeIntent.create(
                        strategy_id=self.strategy_id, symbol=sym,
                        payload=CloseLegPayload(
                            underlying_symbol=sym,
                            contract=new_leap,
                            position_intent=PositionIntent.BUY_TO_OPEN,
                            qty=qty,
                        ),
                        tags=("leap_roll_bto",),
                    ))
                    # Open new NEAR
                    if short_key:
                        new_near = self._selector.select_near(
                            str(sym), list(chains[short_key]),
                            float(new_leap.contract.strike),
                            spot_price=spot,
                            leap_expiry=new_leap.contract.expiry,
                        )
                        if new_near:
                            logger.info("LEAP roll — new NEAR | symbol=%s new=%s", sym, new_near.option_symbol)
                            intents.append(TradeIntent.create(
                                strategy_id=self.strategy_id, symbol=sym,
                                payload=CloseLegPayload(
                                    underlying_symbol=sym,
                                    contract=new_near,
                                    position_intent=PositionIntent.SELL_TO_OPEN,
                                    qty=qty,
                                ),
                                tags=("leap_roll_sto_near",),
                            ))
                        else:
                            logger.warning("LEAP roll — no new NEAR found | symbol=%s", sym)
        return intents

    def _intents_near_rolls(
        self,
        sym: Symbol,
        snapshot: CycleSnapshot,
        holdings,
        spot: float,
    ) -> List[TradeIntent]:
        roll_cfg = self.config.roll
        intents: List[TradeIntent] = []

        leap_expiry: Optional[datetime] = None
        if holdings.leap_position:
            try:
                from src.risk.pmcc_sizer import parse_osi
                leap_expiry = parse_osi(str(holdings.leap_position.get("symbol", ""))).expiry
            except Exception:
                pass

        for near in holdings.near_positions:
            near_sym    = str(near.get("symbol", ""))
            near_dte    = near.get("derived_dte") or 999
            near_strike = near.get("derived_strike") or 0.0
            if not near_sym:
                continue

            roll_near = False
            reason    = ""

            if near_dte <= roll_cfg.near_dte_threshold:
                roll_near, reason = True, f"DTE {near_dte} <= {roll_cfg.near_dte_threshold}"

            if not roll_near:
                cost_basis   = float(near.get("cost_basis")   or 0.0)
                market_value = float(near.get("market_value") or 0.0)
                if cost_basis < 0:
                    premium   = abs(cost_basis)
                    current   = abs(market_value)
                    if premium > 0:
                        captured = (premium - current) / premium
                        if captured >= roll_cfg.near_profit_pct:
                            roll_near = True
                            reason    = f"profit {captured:.1%} >= {roll_cfg.near_profit_pct:.1%}"

            if not roll_near and near_strike > 0 and spot > 0:
                if (spot / near_strike) >= roll_cfg.near_strike_proximity:
                    roll_near = True
                    reason    = (
                        f"spot {spot:.2f} >= {roll_cfg.near_strike_proximity:.1%} "
                        f"of strike {near_strike:.2f}"
                    )

            if not roll_near:
                logger.info("NEAR stable | symbol=%s near=%s dte=%d", sym, near_sym, near_dte)
                continue

            logger.info("Rolling NEAR | symbol=%s near=%s reason=%s", sym, near_sym, reason)

            chains    = snapshot.option_chains
            short_key = _find_key(chains, str(sym), "shorts")
            if short_key is None:
                logger.warning("Shorts chain missing for roll | symbol=%s", sym)
                # Emit close-only — will re-sell next cycle
                intents.append(TradeIntent.create(
                    strategy_id=self.strategy_id, symbol=sym,
                    payload=CloseLegPayload(
                        underlying_symbol=sym,
                        contract=_make_dummy_selected(near_sym),
                        position_intent=PositionIntent.BUY_TO_CLOSE,
                        qty=1,
                    ),
                    tags=("roll_btc_only",),
                ))
                continue

            new_near = self._selector.select_near(
                str(sym), list(chains[short_key]), 0,
                spot_price=spot,
                leap_expiry=leap_expiry,
            )
            if new_near is None:
                logger.warning("No new NEAR for roll | symbol=%s", sym)
                intents.append(TradeIntent.create(
                    strategy_id=self.strategy_id, symbol=sym,
                    payload=CloseLegPayload(
                        underlying_symbol=sym,
                        contract=_make_dummy_selected(near_sym),
                        position_intent=PositionIntent.BUY_TO_CLOSE,
                        qty=1,
                    ),
                    tags=("roll_btc_only",),
                ))
                continue

            logger.info("Roll | symbol=%s btc=%s sto=%s", sym, near_sym, new_near.option_symbol)
            intents.append(TradeIntent.create(
                strategy_id=self.strategy_id, symbol=sym,
                payload=RollNearPayload(
                    underlying_symbol=sym,
                    close=_make_dummy_selected(near_sym),
                    open=new_near,
                    max_net_debit=self.config.roll.near_max_roll_debit,
                ),
                tags=("roll_near",),
            ))

        return intents

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
            payload=CloseLegPayload(
                underlying_symbol=sym,
                contract=_make_dummy_selected(near_sym),
                position_intent=PositionIntent.BUY_TO_CLOSE,
                qty=1,
            ),
        )]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_key(chains, sym_str: str, request_id: str):
    for k in chains:
        if str(k[0]).upper() == sym_str.upper() and k[1] == request_id:
            return k
    return None


def _get_spot(snapshot: CycleSnapshot, sym_str: str) -> float:
    for k, v in snapshot.asset_quotes.items():
        if str(k).upper() == sym_str.upper():
            return v.mid or v.ask or v.bid or 0.0
    return 0.0


def _make_dummy_selected(option_symbol: str) -> SelectedOption:
    """
    Build a SelectedOption from a known OSI symbol when the full contract
    object isn't available (e.g. closing an existing position by symbol).
    The contract fields are derived from OSI parsing; Greeks/quote not needed.
    """
    from src.domain.orders import OptionContract, OptionRight
    from src.risk.pmcc_sizer import parse_osi
    from decimal import Decimal

    try:
        parsed = parse_osi(option_symbol)
        contract = OptionContract(
            underlying=parsed.underlying,
            expiry=parsed.expiry,
            strike=parsed.strike,
            right=OptionRight.CALL if parsed.right == "C" else OptionRight.PUT,
            option_symbol=option_symbol,
        )
    except ValueError:
        from datetime import datetime
        from src.domain.orders import OptionContract, OptionRight
        contract = OptionContract(
            underlying="",
            expiry=datetime.min,
            strike=Decimal("0"),
            right=OptionRight.CALL,
            option_symbol=option_symbol,
        )
    return SelectedOption(option_symbol=option_symbol, contract=contract)