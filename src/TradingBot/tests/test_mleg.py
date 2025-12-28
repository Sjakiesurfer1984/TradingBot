#!/usr/bin/env python
"""
test_mleg.py

This script demonstrates submitting an Iron Condor multi-leg order via Alpaca's API using alpaca-py.
An Iron Condor in this example consists of four legs:
  1. Sell an OTM call (leg to collect premium),
  2. Buy a further OTM call (leg to limit risk on the upside),
  3. Sell an OTM put (leg to collect premium),
  4. Buy a further OTM put (leg to limit risk on the downside).

Each leg is constructed as an OptionLegRequest object.
The order payload is then wrapped in an OrderRequest (a Pydantic model) so that it's validated before submission.

Usage:
    python -m TradingBot.tests.test_mleg
"""

import time
from TradingBot.config import ALPACA_CONFIG
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.requests import OrderRequest, OptionLegRequest, LimitOrderRequest


def main():
    # Retrieve API credentials from the config file
    API_KEY = ALPACA_CONFIG["API_KEY"]
    API_SECRET = ALPACA_CONFIG["API_SECRET"]
    PAPER = ALPACA_CONFIG["PAPER"]

    # Create the trading client (paper trading environment)
    trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    # Manually build the multi-leg order legs using OptionLegRequest, for testing purposes. ENSURE THE SYMBOLS ARE VALID. Verify this via the online Alpaca dashboard.
    # Each leg is defined with a symbol, side, and ratio_qty.
    # Adjust the option_symbol values to valid contracts for your account.
    order_legs_m = [
        OptionLegRequest(
            symbol="SPY250311C00605000",  # Option symbol for the call you sell
            side=OrderSide.SELL.value,
            ratio_qty=1,
        ),
        OptionLegRequest(
            symbol="SPY250311C00610000",  # Option symbol for the call you buy
            side=OrderSide.BUY.value,
            ratio_qty=1,
        ),
        OptionLegRequest(
            symbol="SPY250311P00579000",  # Option symbol for the put you sell
            side=OrderSide.SELL.value,
            ratio_qty=1,
        ),
        OptionLegRequest(
            symbol="SPY250311P00572000",  # Option symbol for the put you buy
            side=OrderSide.BUY.value,
            ratio_qty=1,
        )
    ]

    # Build the multi-leg order payload.
    # Order for the iron condor
    try:
        req = LimitOrderRequest(
            qty=50,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            legs=order_legs_m,
            limit_price=0  # i.e., for a net price of 0
        )
        res = trading_client.submit_order(req)
        print(f"Order response: {res}")
    except Exception as e:
        print(f"Error submitting multi-leg order: {e}")


if __name__ == "__main__":
    main()
