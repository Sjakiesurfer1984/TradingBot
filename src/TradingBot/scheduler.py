# scheduler.py

from dataclasses import dataclass
from datetime import datetime, timedelta
import time
from typing import Callable

# Use an absolute import from TradingBot.logger
from TradingBot.logger import setup_logger

logger = setup_logger("Scheduler")


@dataclass
class Scheduler:
    """
    A simple scheduler that runs a given task at fixed intervals (in minutes).

    Attributes:
        interval_minutes (int): The time interval in minutes at which the task repeats.
    """
    interval_minutes: int

    def run(self, task: Callable) -> None:
        """
        Run the specified task at regular intervals.

        Args:
            task (Callable): The function to be executed periodically.
        """
        logger.debug(
            f"Starting scheduler with interval: {self.interval_minutes} minutes.")
        interval = timedelta(minutes=self.interval_minutes)
        while True:
            start_time = datetime.now()
            logger.debug(f"Starting task at {start_time}")
            task()
            elapsed_time = datetime.now() - start_time
            logger.debug(f"Task completed. Elapsed time: {elapsed_time}")
            # Ensure we sleep for the remainder of the interval
            time.sleep(max(0, (interval - elapsed_time).total_seconds()))
