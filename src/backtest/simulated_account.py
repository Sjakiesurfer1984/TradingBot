from __future__ import annotations

"""
src/backtest/simulated_account.py
----------------------------------
Tracks simulated cash, positions, and buying power as the backtest
processes fills. This is the single source of truth for account state
during a backtest run.

Design rules:
- All prices are per-share (options = per contract share, i.e. price * 100 for cost).
- Positions are stored as a dict keyed by OSI symbol.
- Buying power = cash (simplified: no margin model, options are cash-settled debit).
- cost_basis on short positions is stored as negative (credit received).
- market_value is recomputed each cycle from the current chain snapshot.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.utilities.logger import setup_logger

logger = setup_logger("SimulatedAccount")


@dataclass
class SimulatedPosition:
    symbol:         str
    asset_class:    str           # "us_option"
    underlying_symbol: str
    qty:            float         # positive = long, negative = short
    cost_basis:     float         # total cost basis (negative for credits received)
    market_value:   float         # updated each cycle from chain
    side:           str           # "long" | "short"

    def as_dict(self) -> Dict[str, Any]:
        """Return a position dict in the same shape AlpacaBroker.get_positions() returns."""
        return {
            "symbol":           self.symbol,
            "asset_class":      self.asset_class,
            "underlying_symbol": self.underlying_symbol,
            "qty":              str(self.qty),
            "side":             self.side,
            "cost_basis":       str(self.cost_basis),
            "market_value":     str(self.market_value),
        }


@dataclass
class SimulatedAccount:
    """
    Tracks cash and open positions for the backtest.

    Buying power is modelled simply as cash. Options are assumed to be
    cash-secured: buying a LEAP debits cash, selling a NEAR credits cash.

    Slippage is applied at fill time: buys fill at ask, sells fill at bid.
    This is conservative and realistic for SPY options.
    """

    initial_cash:   float
    _cash:          float                             = field(init=False)
    _positions:     Dict[str, SimulatedPosition]      = field(default_factory=dict, init=False)
    _trade_log:     List[Dict[str, Any]]              = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._cash = self.initial_cash

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def equity(self) -> float:
        """Cash + sum of all position market values."""
        return self._cash + sum(p.market_value for p in self._positions.values())

    @property
    def option_buying_power(self) -> float:
        """
        Simplified: buying power = cash.
        Short positions have already credited cash at fill time.
        """
        return max(0.0, self._cash)

    def positions(self) -> List[Dict[str, Any]]:
        return [p.as_dict() for p in self._positions.values()]

    def trade_log(self) -> List[Dict[str, Any]]:
        return list(self._trade_log)

    # ------------------------------------------------------------------
    # Fill processing
    # ------------------------------------------------------------------

    def fill_buy(
        self,
        *,
        symbol:     str,
        underlying: str,
        qty:        int,
        fill_price: float,   # per-share price (option premium)
        date_str:   str,
    ) -> bool:
        """
        Process a buy fill (BTO or BTC).

        BTO: opens a new long position, debits cash.
        BTC: closes an existing short position.

        Returns True if the fill was accepted, False if insufficient cash.
        """
        total_cost = fill_price * qty * 100  # options = 100 shares per contract

        if symbol in self._positions and self._positions[symbol].qty < 0:
            # BTC — closing a short
            pos = self._positions[symbol]
            close_cost = fill_price * abs(qty) * 100
            self._cash -= close_cost  # paying to close

            new_qty = pos.qty + qty
            if abs(new_qty) < 0.001:
                # Fully closed
                realized_pnl = abs(pos.cost_basis) - close_cost
                logger.info(
                    "BTC fill | symbol=%s qty=%d fill=%.2f cost=%.2f pnl=%.2f cash=%.2f",
                    symbol, qty, fill_price, close_cost, realized_pnl, self._cash,
                )
                self._trade_log.append({
                    "date": date_str, "action": "BTC", "symbol": symbol,
                    "qty": qty, "fill_price": fill_price,
                    "realized_pnl": realized_pnl,
                })
                del self._positions[symbol]
            else:
                pos.qty = new_qty
                pos.cost_basis = pos.cost_basis * (new_qty / pos.qty)
            return True

        # BTO — opening a long
        if total_cost > self._cash:
            logger.warning(
                "BTO rejected — insufficient cash | symbol=%s cost=%.2f cash=%.2f",
                symbol, total_cost, self._cash,
            )
            return False

        self._cash -= total_cost

        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.cost_basis -= total_cost  # debit
            pos.qty        += qty
        else:
            self._positions[symbol] = SimulatedPosition(
                symbol=symbol,
                asset_class="us_option",
                underlying_symbol=underlying,
                qty=float(qty),
                cost_basis=-total_cost,  # negative = debit paid
                market_value=0.0,
                side="long",
            )

        logger.info(
            "BTO fill | symbol=%s qty=%d fill=%.2f cost=%.2f cash=%.2f",
            symbol, qty, fill_price, total_cost, self._cash,
        )
        self._trade_log.append({
            "date": date_str, "action": "BTO", "symbol": symbol,
            "qty": qty, "fill_price": fill_price, "cost": total_cost,
        })
        return True

    def fill_sell(
        self,
        *,
        symbol:     str,
        underlying: str,
        qty:        int,
        fill_price: float,
        date_str:   str,
    ) -> bool:
        """
        Process a sell fill (STO or STC).

        STO: opens a new short position, credits cash.
        STC: closes an existing long position.

        Returns True always (sells are always accepted — you own what you sell).
        """
        total_credit = fill_price * qty * 100

        if symbol in self._positions and self._positions[symbol].qty > 0:
            # STC — closing a long
            pos = self._positions[symbol]
            self._cash += total_credit

            new_qty = pos.qty - qty
            realized_pnl = total_credit + pos.cost_basis  # cost_basis is negative
            if abs(new_qty) < 0.001:
                logger.info(
                    "STC fill | symbol=%s qty=%d fill=%.2f credit=%.2f pnl=%.2f cash=%.2f",
                    symbol, qty, fill_price, total_credit, realized_pnl, self._cash,
                )
                self._trade_log.append({
                    "date": date_str, "action": "STC", "symbol": symbol,
                    "qty": qty, "fill_price": fill_price,
                    "realized_pnl": realized_pnl,
                })
                del self._positions[symbol]
            else:
                pos.qty = new_qty
            return True

        # STO — opening a short
        self._cash += total_credit

        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.cost_basis += total_credit  # credit received
            pos.qty        -= qty
        else:
            self._positions[symbol] = SimulatedPosition(
                symbol=symbol,
                asset_class="us_option",
                underlying_symbol=underlying,
                qty=float(-qty),
                cost_basis=total_credit,   # positive = credit received
                market_value=0.0,
                side="short",
            )

        logger.info(
            "STO fill | symbol=%s qty=%d fill=%.2f credit=%.2f cash=%.2f",
            symbol, qty, fill_price, total_credit, self._cash,
        )
        self._trade_log.append({
            "date": date_str, "action": "STO", "symbol": symbol,
            "qty": qty, "fill_price": fill_price, "credit": total_credit,
        })
        return True

    def update_market_values(self, prices: Dict[str, float]) -> None:
        """
        Update market_value on all open positions from current chain prices.
        prices: { osi_symbol -> current_mid_price }
        """
        for sym, pos in self._positions.items():
            price = prices.get(sym)
            if price is not None:
                pos.market_value = price * abs(pos.qty) * 100 * (
                    1 if pos.qty > 0 else -1
                )

    def summary(self, date_str: str) -> Dict[str, Any]:
        return {
            "date":              date_str,
            "cash":              round(self._cash, 2),
            "equity":            round(self.equity, 2),
            "option_buying_power": round(self.option_buying_power, 2),
            "open_positions":    len(self._positions),
        }
