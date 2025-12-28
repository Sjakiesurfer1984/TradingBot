# TradingBot/strategies/poormans_covered_call.py
#
# Purpose
# - Implements a Poor Man's Covered Call (PMCC).
# - Buys a long-dated call (LEAPS) and sells a near-term call against it.
#
# Open-order deduplication
# - The first thing execute() does is query open orders.
# - If an open order already exists for the same underlying, execution returns immediately.
#
# Design note
# - The cleanest dedupe method is tagging orders with a deterministic client_order_id.
# - We perform two checks:
#   (a) Exact client_order_id match (fast and unambiguous for orders we created)
#   (b) Fallback heuristic scanning of order legs (useful if older orders were not tagged)

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from TradingBot.brokers.broker_interface import BrokerInterface
from TradingBot.bot_state import BotState
from TradingBot.domain.orders import MultiLegLimitOrder, OptionLeg, OrderSide, TimeInForce
from TradingBot.logger import setup_logger
from TradingBot.strategies.strategy_interface import Strategy
from TradingBot.utilities.utils import build_expiration_range, build_strike_range, select_option_by_delta

logger = setup_logger("PoormansCoveredCall")


@dataclass
class PoormansCoveredCall(Strategy):
    """
    Poor Man's Covered Call (PMCC).

    Strategy concept
    - Long: a higher-delta call with longer time to expiry (often called a LEAPS call).
    - Short: a lower-delta call with near-term expiry, sold against the long call.

    Practical goal
    - The long call approximates owning shares with less capital.
    - The short call generates premium and partially offsets time decay.

    Deduplication behaviour
    - If there is already an open order that relates to this underlying symbol, do nothing.
    - This prevents repeated scheduler triggers from placing duplicate orders while an order is still open.

    Important limitation
    - This class only deduplicates by open orders, not by open positions.
      If you want "only one PMCC position per symbol", you should also check existing positions.
    """

    broker: BrokerInterface
    symbol: str

    target_leap_dte: int
    target_leap_delta: float
    leap_strike_multipliers: Tuple[float, float]

    target_near_dte: int
    target_near_delta: float
    near_strike_multipliers: Tuple[float, float]

    # A stable prefix used to identify orders created by this strategy.
    # Using a deterministic value makes deduplication reliable and avoids heuristics.
    _client_order_prefix: str = "PMCC"

    def __post_init__(self) -> None:
        """
        Validate inputs immediately after dataclass initialisation.

        Why validate here
        - Fail fast during bot startup rather than failing mid-trade.
        - Makes configuration mistakes obvious and easier to debug.
        """
        if not self.symbol or not isinstance(self.symbol, str):
            raise ValueError("symbol must be a non-empty string.")

        self._validate_dte(self.target_leap_dte, "target_leap_dte")
        self._validate_dte(self.target_near_dte, "target_near_dte")

        self._validate_delta(self.target_leap_delta, "target_leap_delta")
        self._validate_delta(self.target_near_delta, "target_near_delta")

        self._validate_multipliers(self.leap_strike_multipliers, "leap_strike_multipliers")
        self._validate_multipliers(self.near_strike_multipliers, "near_strike_multipliers")

    @staticmethod
    def _validate_dte(value: int, field_name: str) -> None:
        """
        DTE (days to expiry) must be a positive integer.

        Why this matters
        - Expiration range builders assume an integer day count.
        - Non-positive values cause nonsensical expiration windows.
        """
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} must be a positive integer.")

    @staticmethod
    def _validate_delta(value: float, field_name: str) -> None:
        """
        Validate a delta target.

        Delta constraints
        - We assume a normalised delta in (0, 1] for calls.
        - A delta of 0 implies no sensitivity to the underlying, which is not useful.
        """
        if not isinstance(value, (float, int)):
            raise ValueError(f"{field_name} must be a float in (0, 1].")
        if value <= 0.0 or value > 1.0:
            raise ValueError(f"{field_name} must be in (0, 1].")

    @staticmethod
    def _validate_multipliers(value: Tuple[float, float], field_name: str) -> None:
        """
        Strike multipliers define a price window around the underlying.

        Example
        - If underlying is 100 and multipliers are (0.90, 1.10),
          strikes are allowed in the range [90, 110].

        Constraints
        - Both must be positive.
        - Lower must be strictly less than upper to define a non-empty range.
        """
        if not isinstance(value, tuple) or len(value) != 2:
            raise ValueError(f"{field_name} must be a tuple of (lower, upper).")

        lower: float
        upper: float
        lower, upper = value

        if not isinstance(lower, (float, int)) or not isinstance(upper, (float, int)):
            raise ValueError(f"{field_name} must contain numeric values.")
        if lower <= 0 or upper <= 0 or lower >= upper:
            raise ValueError(f"{field_name} must satisfy 0 < lower < upper.")

    def get_asset_price(self) -> float:
        """
        Fetch the underlying price and validate it.

        Why validate
        - Strike range selection depends on a sensible underlying price.
        - A non-positive or non-numeric value would cause invalid ranges and poor trade selection.
        """
        price: Any = self.broker.get_asset_price(self.symbol)

        if not isinstance(price, (float, int)):
            raise RuntimeError(f"Broker returned non-numeric price for {self.symbol}: {price}")
        if price <= 0:
            raise RuntimeError(f"Broker returned non-positive price for {self.symbol}: {price}")

        return float(price)

    def _fetch_option_chain(
        self,
        strike_range: Tuple[float, float],
        expiration_range: Tuple[date, date],
    ) -> pd.DataFrame:
        """
        Fetch an option chain for calls within the given strike and expiration ranges.

        Assumptions
        - The broker returns a pandas DataFrame.
        - If the broker returns None or a non-DataFrame, something is wrong upstream.

        Why keep this as a dedicated method
        - It centralises validation and keeps execute() focused on the strategy flow.
        """
        chain: Any = self.broker.get_option_chain(
            symbol=self.symbol,
            tipo="call",
            strike=strike_range,
            expiration=expiration_range,
        )

        if chain is None or not isinstance(chain, pd.DataFrame):
            raise RuntimeError("Broker returned an invalid option chain (expected a DataFrame).")

        return chain

    @staticmethod
    def _require_columns(df: pd.DataFrame, required: Tuple[str, ...], context: str) -> None:
        """
        Ensure the DataFrame includes the columns the strategy depends on.

        Why this matters
        - Many runtime failures in trading systems are schema drift issues.
        - It is better to fail loudly with a clear message than to trade on missing data.
        """
        missing: List[str] = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"{context} is missing required columns: {missing}. Present: {list(df.columns)}")

    def _calculate_order_quantity(self, leap_option: pd.Series, budget_fraction: float = 0.08) -> int:
        """
        Calculate the number of contracts to trade.

        Sizing approach
        - Uses a simple fraction of options buying power.
        - Divides budget by estimated cost per LEAPS contract (ask_price * 100).

        Why it is conservative
        - Ask price is used for cost estimation, which errs toward a higher cost.
        - Floors to an integer number of contracts to avoid over-allocating.

        Behaviour when data is missing
        - If buying power is not positive, returns 0.
        - If ask_price is missing or non-positive, returns 0.
        """
        if budget_fraction <= 0.0 or budget_fraction > 1.0:
            raise ValueError("budget_fraction must be in (0, 1].")

        buying_power: Any = self.broker.get_option_buying_power()
        if not isinstance(buying_power, (float, int)) or buying_power <= 0:
            logger.warning("Options buying power is not positive. Quantity set to 0.")
            return 0

        ask_price_raw: Any = leap_option.get("ask_price", 0.0)
        ask_price: float = float(ask_price_raw or 0.0)
        if ask_price <= 0:
            logger.warning("LEAPS ask_price is missing or non-positive. Quantity set to 0.")
            return 0

        cost_per_contract: float = ask_price * 100.0
        budget: float = float(buying_power) * float(budget_fraction)
        qty: int = int(budget // cost_per_contract)

        logger.debug(
            "Sizing: buying_power=%s, budget_fraction=%s, budget=%s, ask_price=%s, cost_per_contract=%s, qty=%s",
            buying_power,
            budget_fraction,
            budget,
            ask_price,
            cost_per_contract,
            qty,
        )
        return max(qty, 0)

    def _make_client_order_id(self) -> str:
        """
        Create a deterministic client_order_id for this strategy and underlying.

        Why this matters
        - It enables a quick and reliable "did we already place an order for this symbol?" check.
        - It avoids depending on the broker's internal order IDs, which are not deterministic.
        """
        sym: str = self.symbol.strip().upper()
        return f"{self._client_order_prefix}:{sym}"

    @staticmethod
    def _safe_str(value: Any) -> str:
        """
        Convert a value to a safe string.

        Why this helper exists
        - Broker payloads can contain None or unexpected types.
        - This keeps string comparisons robust and avoids TypeError.
        """
        return "" if value is None else str(value)

    def _order_mentions_underlying(self, order: Dict[str, Any]) -> bool:
        """
        Heuristic fallback to detect whether an order likely relates to this underlying.

        Why a fallback is needed
        - Older orders might not have a client_order_id.
        - Different broker endpoints can return payloads with slightly different shapes.

        Heuristic used
        - Many option symbols begin with the underlying ticker, for example:
          SPY260206C00718000 begins with SPY.

        Safety behaviour
        - Missing keys are treated as "not a match".
        - We do not raise on missing structure because this is a best-effort check.
        """
        underlying: str = self.symbol.strip().upper()

        # Single-leg orders sometimes expose a top-level 'symbol'.
        symbol_field: str = self._safe_str(order.get("symbol")).upper()
        if symbol_field.startswith(underlying):
            return True

        # Multi-leg orders often contain legs under 'legs'.
        legs: Any = order.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                leg_symbol: str = self._safe_str(leg.get("symbol")).upper()
                if leg_symbol.startswith(underlying):
                    return True

        return False

    def _has_open_order_for_underlying(self) -> bool:
        """
        Return True if any open order relates to this underlying symbol.

        Priority of checks
        - First check deterministic client_order_id match (the strongest signal).
        - Then use the heuristic symbol inspection as a fallback.

        Failure policy
        - If the broker fails to return open orders, it is safer to raise than to place duplicates.
          Duplicate orders are typically harder to unwind than a deliberate stop due to an error.
        """
        open_orders: List[Dict[str, Any]] = self.broker.get_open_orders()
        expected_id: str = self._make_client_order_id()

        for order in open_orders:
            if not isinstance(order, dict):
                continue

            client_order_id: str = self._safe_str(order.get("client_order_id"))
            if client_order_id == expected_id:
                return True

            # If you later decide that any PMCC order should block new PMCC orders,
            # you could check by prefix instead of equality:
            # if client_order_id.startswith(self._client_order_prefix + ":"):
            #     return True

            if self._order_mentions_underlying(order):
                return True

        return False

    def _build_order_request(
        self,
        quantity: int,
        leap_option: pd.Series,
        near_option: pd.Series,
    ) -> MultiLegLimitOrder:
        """
        Create the multi-leg order request object.

        Order structure
        - Buy the LEAPS call.
        - Sell the near-term call.
        - Use the same ratio quantity for both legs to create a covered structure.

        Note
        - limit_price is set to 0.0 here, which may indicate "marketable" behaviour depending
          on your broker wrapper.
        - If you want true limit control, compute a net debit from legs (ask minus bid) and set it.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive.")

        leap_symbol: str = str(leap_option["option_symbol"])
        near_symbol: str = str(near_option["option_symbol"])

        legs: List[OptionLeg] = [
            OptionLeg(symbol=leap_symbol, side=OrderSide.BUY, ratio_qty=quantity),
            OptionLeg(symbol=near_symbol, side=OrderSide.SELL, ratio_qty=quantity),
        ]

        return MultiLegLimitOrder(
            qty=quantity,
            legs=legs,
            time_in_force=TimeInForce.DAY,
            limit_price=0.0,
            client_order_id=self._make_client_order_id(),
        )

    def _submit_order(self, order_req: MultiLegLimitOrder) -> None:
        """
        Submit the order via the broker.

        Why logging the response matters
        - Brokers can accept an order but reject a leg, or return warnings.
        - Persisting the response in logs makes later debugging much easier.
        """
        res: Any = self.broker.submit_order(order_req)
        logger.info("Multi-leg order submitted.")
        logger.info("Order response: %s", res)

    def execute(self, state: BotState) -> None:
        """
        Execute one scheduled strategy cycle.

        Execution flow
        - Deduplicate on open orders first to avoid duplicate order placement.
        - Only proceed to refreshing account data and fetching option chains when safe to do so.

        Reason for this ordering
        - Open-order checks are typically fast and reduce unnecessary API calls.
        - Option chain calls are relatively expensive and can be rate-limited.
        """
        logger.debug("Executing PoormansCoveredCall strategy.")

        # Deduplication is the first defence against accidental repeated placement.
        if self._has_open_order_for_underlying():
            logger.info("Open order already exists for %s. Skipping this cycle.", self.symbol)
            return

        # Refresh state after deduplication, because these calls can be expensive and are not needed
        # if we are going to skip execution anyway.
        state.refresh_account_info(self.broker)
        state.refresh_positions(self.broker)

        underlying_price: float = self.get_asset_price()
        logger.debug("Underlying price for %s: %s", self.symbol, underlying_price)

        # LEAPS selection
        leap_strike_range: Tuple[float, float] = build_strike_range(underlying_price, self.leap_strike_multipliers)

        # The expiration window here is target_leap_dte +/- 180 days.
        # This intentionally casts a wide net so the delta-based selector has enough candidates.
        leap_expiration_range: Tuple[date, date] = build_expiration_range(self.target_leap_dte, (180, 180))
        leap_chain: pd.DataFrame = self._fetch_option_chain(leap_strike_range, leap_expiration_range)

        if leap_chain.empty:
            logger.warning("LEAPS option chain is empty. Skipping execution.")
            return

        self._require_columns(
            leap_chain,
            required=("option_symbol", "expiration_date", "delta", "ask_price", "bid_price"),
            context="LEAPS chain",
        )

        leap_option: pd.Series = select_option_by_delta(
            leap_chain,
            target_dte=self.target_leap_dte,
            target_delta=float(self.target_leap_delta),
            sort_desc=False,
        )
        logger.info("Selected LEAPS option:\n%s", leap_option.to_frame().T.to_string(index=False))

        # Near-term selection
        near_strike_range: Tuple[float, float] = build_strike_range(underlying_price, self.near_strike_multipliers)

        # The expiration window here is target_near_dte +/- 7 days.
        # This allows flexibility if the exact DTE is not listed or the chain is sparse.
        near_expiration_range: Tuple[date, date] = build_expiration_range(self.target_near_dte, (7, 7))
        near_chain: pd.DataFrame = self._fetch_option_chain(near_strike_range, near_expiration_range)

        if near_chain.empty:
            logger.warning("Near-term option chain is empty. Skipping execution.")
            return

        self._require_columns(
            near_chain,
            required=("option_symbol", "expiration_date", "delta", "ask_price", "bid_price"),
            context="Near-term chain",
        )

        near_option: pd.Series = select_option_by_delta(
            near_chain,
            target_dte=self.target_near_dte,
            target_delta=float(self.target_near_delta),
            sort_desc=True,
        )
        logger.info("Selected near-term option:\n%s", near_option.to_frame().T.to_string(index=False))

        # Quantity and order placement
        quantity: int = self._calculate_order_quantity(leap_option, budget_fraction=0.15) # we override the standard 8% budget to 15% here. This is the max we are comfortable with.
        # and represents the % of our options buying power we are willing to allocate to this trade.
        if quantity <= 0:
            logger.warning("Calculated quantity is 0. Skipping order submission.")
            return

        order_req: MultiLegLimitOrder = self._build_order_request(quantity, leap_option, near_option)
        self._submit_order(order_req)
