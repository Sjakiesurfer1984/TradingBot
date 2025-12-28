# test_alpaca_chain.py
from TradingBot.config import ALPACA_CONFIG

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from brokers.alpaca_broker import AlpacaBroker  # Adjust import


def main():
    logging.basicConfig(level=logging.DEBUG)

    # Initialize AlpacaBroker
    broker = AlpacaBroker(
        api_key=ALPACA_CONFIG["API_KEY"],
        api_secret=ALPACA_CONFIG["API_SECRET"],
        paper=ALPACA_CONFIG["PAPER"]
    )

    # Example parameters
    symbol = "SPY"
    strike = (244.0, 282.0)  # Example strike range
    now = datetime.now(tz=ZoneInfo("America/New_York"))
    expiration = (now.date() + timedelta(days=10),
                  now.date() + timedelta(days=90))

    # Call the get_option_chain method
    df = broker.get_option_chain(
        symbol, tipo="call", strike=strike, expiration=expiration)

    print("Returned option chain DataFrame:")
    print(df.head(20))


if __name__ == "__main__":
    main()
