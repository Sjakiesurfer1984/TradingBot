# main.py
"""
Main entry point for the TradingBot project.

This script initializes the trading bot components (broker, state, scheduler, and strategy)
and starts a recurring task to execute the PoormansCoveredCall strategy at regular intervals.

Only high-level target parameters (target DTE and target delta) are passed.
"""

from TradingBot.config import ALPACA_CONFIG
from TradingBot.brokers.alpaca_broker import AlpacaBroker
from TradingBot.strategies.poormans_covered_call import PoormansCoveredCall
from TradingBot.bot_state import BotState
from TradingBot.scheduler import Scheduler
from TradingBot.logger import setup_logger
from datetime import datetime

logger = setup_logger("Main")


def main() -> None:
    logger.debug("Initializing bot components.")

    # validate configuration
    try:
        from TradingBot.config import validate_alpaca_config
        validate_alpaca_config(ALPACA_CONFIG)
        logger.debug("Alpaca configuration validated successfully.")
    except ValueError as e:
        logger.error(f"Configuration validation error: {e}")
        return
    
    # Initialize the broker using configuration values.
    alpaca = AlpacaBroker(
        api_key=ALPACA_CONFIG["api_key"],
        api_secret=ALPACA_CONFIG["api_secret"],
        paper=ALPACA_CONFIG["paper"]
    )

    state = BotState()
    scheduler = Scheduler(interval_minutes=5)
    # This bot will implement a Poorman's Covered Call strategy, which consists of selling a near-term call option
    # and buying a LEAP call option to cover the potential assignment risk.

    # Define strategy configuration as a dictionary.
    # we use the target DTE to construct the expiration ranges for LEAP and near-term options.
    # we use the target delta to filter the candidate call options
    # The multipliers are used to compute the strike price ranges for LEAP and near-term options, based on the underlying asset's price.
    # Question: Isn't it odd, and doesnt it violate the DRY principle, to have the same parameters in the strategy_config dictionary and in the PoormansCoveredCall constructor?
    # And, doesn't it violate the abstraction principle, having to define what strike tolerances we need? In the end,
    # the strategy should be able to determine the strike tolerances by itself, based on the target delta and underlying asset price.
    strategy_config = {

        # leap call parameters
        "target_leap_dte": 420,       # Target DTE for LEAP (~18 months)
        "target_leap_delta": 0.80,      # Target delta for LEAP option. THis should be 0.8. 
        # +/- 20% of the target strike price
        "leap_strike_multipliers": (0.8, 1.2),
        # near call parameters
        "target_near_dte": 45,        # Target DTE for near call (45 days)
        "target_near_delta": 0.17,     # Target delta for near call.
        # +/- 20% of the target strike price
        "near_strike_multipliers": (0.8, 1.2)
    }

    # Instantiate the strategy using argument unpacking.
    strategy = PoormansCoveredCall(
        broker=alpaca,
        symbol="SPY",
        # the ** indicates that we are unpacking the dictionary into keyword arguments, which is what the constructor expects.
        **strategy_config
    )

    def bot_task() -> None:
        logger.debug("Running bot task.")
        try:
            strategy.execute(state)
            logger.info("Bot task execution complete.")
        except Exception as e:
            logger.error(f"Error in bot task execution: {e}")

    logger.debug("Starting task scheduler.")
    scheduler.run(bot_task)

def run() -> None:
    main()

if __name__ == "__main__":
    run()

