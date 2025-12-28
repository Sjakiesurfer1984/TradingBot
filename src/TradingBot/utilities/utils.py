# # TradingBot/utilities/option_utils.py

# """
# Utility functions for selecting options from an options chain DataFrame.

# This module provides a generic helper function to filter and select an option
# based on various criteria such as expiration dates, strike price ranges, and delta values.
# """

# from typing import Optional, Tuple
# from datetime import date, timedelta
# import pandas as pd
# from datetime import date
# from typing import Tuple, Optional


# def select_option(
#     options_chain: pd.DataFrame,
#     underlying_price: float,
#     min_expiration_date: Optional[date] = None,
#     max_expiration_date: Optional[date] = None,
#     delta_range: Tuple[float, float] = (0.20, 0.30),
#     strike_multiplier: Optional[float] = None,
#     strike_range: Optional[Tuple[float, float]] = None,
#     delta_sort_desc: bool = False,
# ) -> pd.Series:
#     """
#     A generic function to filter and select an option from the options chain.

#     Args:
#         options_chain (pd.DataFrame): A DataFrame containing columns like 'expiration_date',
#             'strike_price', 'delta', etc.
#         underlying_price (float): Current price of the underlying asset.
#         min_expiration_date (Optional[date]): Earliest allowed expiration date (if any).
#         max_expiration_date (Optional[date]): Latest allowed expiration date (if any).
#         delta_range (Tuple[float, float]): A tuple (min_delta, max_delta) specifying acceptable delta bounds.
#         strike_multiplier (Optional[float]): If set, computes a target strike as underlying_price * strike_multiplier.
#             If provided, a ±2% band is applied around this target.
#         strike_range (Optional[Tuple[float, float]]): If set, a direct range (min_strike, max_strike) for the strike price.
#         delta_sort_desc (bool): Whether to sort by delta descending (True) or ascending (False) when selecting the best candidate.

#     Returns:
#         pd.Series: A single row (Series) from the filtered DataFrame representing the selected option.

#     Raises:
#         ValueError: If no suitable option is found after applying the filters.
#     """
#     df = options_chain.copy()

#     # 1. Filter by expiration date
#     if min_expiration_date or max_expiration_date:
#         if min_expiration_date:
#             df = df[df["expiration_date"] >= min_expiration_date]
#         if max_expiration_date:
#             df = df[df["expiration_date"] <= max_expiration_date]

#     # 2. Filter by strike price
#     if strike_range is not None:
#         min_strike, max_strike = strike_range
#     elif strike_multiplier is not None:
#         target_strike = underlying_price * strike_multiplier
#         min_strike = target_strike * 0.8
#         max_strike = target_strike * 1.2
#     else:
#         # If neither strike_range nor strike_multiplier is provided, no strike filtering is applied.
#         min_strike, max_strike = None, None

#     if min_strike is not None and max_strike is not None:
#         df = df[(df["strike_price"] >= min_strike) &
#                 (df["strike_price"] <= max_strike)]

#     # 3. Filter by delta range
#     min_delta, max_delta = delta_range
#     df = df[(df["delta"] >= min_delta) & (df["delta"] <= max_delta)]

#     if df.empty:
#         raise ValueError("No options found after applying filters.")

#     # 4. Sort by delta
#     # For a short call, ascending (lowest delta first); for a LEAP, descending (highest delta first)
#     df = df.sort_values(by="delta", ascending=not delta_sort_desc)

#     # Return the first candidate after sorting
#     return df.iloc[0]


"""
Utility functions for option selection.
"""

from datetime import date, timedelta, datetime
from turtle import up
from typing import Tuple, Optional
import pandas as pd
from TradingBot.logger import setup_logger

logger = setup_logger("Utils")


def build_strike_range(underlying_price: float, strike_multipliers: Optional[Tuple[float, float]]) -> Tuple[float, float]:
    """
    Compute a strike price range from the underlying asset's price using fixed multipliers.

    Args:
        underlying_price (float): Current price of the underlying asset.
        strike_range (Tuple[float, float]): A tuple (min_tolerance, max_tolerance) for the strike price.

    Returns:
        Tuple[float, float]: A tuple (min_strike, max_strike) rounded to two decimals.
    """
    # if no strike multipliers are provided, use the default range (0.5, 1.5)
    # this allows for a ±50% band around the underlying price, and gives us flexibility to adjust the range if wanted.
    if not isinstance(strike_multipliers, tuple):
        strike_multipliers = (0.5, 1.5)
    else:
        if strike_multipliers[0] > strike_multipliers[1]:
            raise ValueError("Invalid strike multiplier range.")
        elif strike_multipliers[0] <= 0 or strike_multipliers[1] <= 0:
            raise ValueError(
                "Strike multipliers must be positive values and larger than 0.")
        elif underlying_price <= 0:
            raise ValueError("Underlying price must be a positive value.")
        elif strike_multipliers[0] == 1 and strike_multipliers[1] == 1:
            return (underlying_price, underlying_price)
        elif strike_multipliers[0] <= 0.5 or strike_multipliers[1] >= 2:
            min_strike = round(underlying_price * strike_multipliers[0], 2)
            max_strike = round(underlying_price * strike_multipliers[1], 2)
            raise ValueError(
                "Strike multipliers must be within the range (0.5, 2). Setting default range (0.5, 1.5).")
        else:
            min_strike = round(underlying_price * strike_multipliers[0], 2)
            max_strike = round(underlying_price * strike_multipliers[1], 2)
    # do we have to set the return as a tuple? Or is this implicit?
    return (min_strike, max_strike)


def build_expiration_range(target_dte: int, days_range: Tuple[int, int]) -> Tuple[date, date]:
    """
    Compute the expiration date range around a target days-to-expiration (DTE).

    The target expiration date is calculated as today + target_dte.
    Then the range is defined as:
        - min_expiration = target expiration - minus_days
        - max_expiration = target expiration + plus_days

    Args:
        target_dte (int): The target days-to-expiration.
        minus_days (int): The number of days to subtract from the target DTE (minimum).
        plus_days (int): The number of days to add to the target DTE (maximum).

    Returns:
        Tuple[date, date]: A tuple (min_expiration, max_expiration) of date objects.
    """
    # Default range if days_range is not provided or invalid.
    if not isinstance(days_range, tuple) or len(days_range) != 2:
        days_range = (180, 180)

    target_date = date.today() + timedelta(days=target_dte)
    min_expiration = target_date - timedelta(days=days_range[0])
    max_expiration = target_date + timedelta(days=days_range[1])
    logger.debug(
        f"Computed expiration range: {min_expiration} to {max_expiration} (target: {target_date})")
    return (min_expiration, max_expiration)


# def select_option_by_delta(
#     options_chain: pd.DataFrame,
#     delta_range: Tuple[float, float],
#     sort_desc: bool = False


# ) -> pd.Series:
#     """
#     Filter and select an option from an options chain DataFrame based on the delta range.

#     Args:
#         options_chain (pd.DataFrame): DataFrame containing option data (must include a 'delta' column).
#         delta_range (Tuple[float, float]): Acceptable delta range (e.g., (0.60, 0.70)).
#         sort_desc (bool): If True, sort by delta in descending order; otherwise ascending.

#     Returns:
#         pd.Series: The selected option (one row of the DataFrame).

#     Raises:
#         ValueError: If no option meets the delta criteria.
#     """
#     df = options_chain.copy()
#     min_delta, max_delta = delta_range
#     df = df[(df["delta"] >= min_delta) & (df["delta"] <= max_delta)]
#     if df.empty:
#         raise ValueError("No options found matching the delta criteria.")
#     df = df.sort_values(by="delta", ascending=not sort_desc)
#     return df.iloc[0]

def select_option_by_delta(
    options_chain: pd.DataFrame,
    target_dte: int,
    target_delta: float,
    sort_desc: bool = False
) -> pd.Series:
    """
    Filter and select an option from an options chain DataFrame based on:
      1. Delta being within the specified delta_range.
      2. The option's expiration date being as close as possible to the target expiration
         (computed as today's date plus target_dte).
      3. The option's delta being as close as possible to the target_delta.

    Args:
        options_chain (pd.DataFrame): DataFrame containing option data (with 'delta' and 'expiration_date').
        delta_range (Tuple[float, float]): Acceptable delta range (e.g., (0.60, 0.70)).
        target_dte (int): Target days-to-expiration (used to compute the target expiration date).
        target_delta (float): The desired delta value.
        sort_desc (bool): If True, sort by delta descending; otherwise ascending.
                         (This flag may be used as a secondary sort if needed.)

    Returns:
        pd.Series: The selected option (one row of the DataFrame).

    Raises:
        ValueError: If no options meet the delta criteria.
    """
    # Compute the target expiration date based on target_dte.
    target_expiration = date.today() + timedelta(days=target_dte)

    df = options_chain.copy()

    # Compute the difference in expiration and delta from target values.
    df["exp_diff"] = df["expiration_date"].apply(
        lambda x: abs((x - target_expiration).days))
    df["delta_diff"] = df["delta"].apply(lambda x: abs(x - target_delta))

    # First, sort by expiration difference (ascending: closest expiration first).
    # Then, sort by delta difference (ascending: delta closest to target).
    df = df.sort_values(by=["exp_diff", "delta_diff"], ascending=[True, True])

    return df.iloc[0]
