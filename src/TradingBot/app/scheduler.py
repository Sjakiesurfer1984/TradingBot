from __future__ import annotations

import time
from dataclasses import dataclass

from TradingBot.utilities.logger import setup_logger
from TradingBot.orchestration.orchestrator import Orchestrator

logger = setup_logger("Scheduler")


@dataclass(frozen=True)
class SchedulerConfig:
    """
    Scheduler configuration.

    cycle_seconds
    - Time between cycle starts.
    - For now we keep it simple: sleep after each cycle completes.
    """

    cycle_seconds: float


class Scheduler:
    """
    Very small scheduler for running Option C cycles repeatedly.

    Design intent
    - Keep scheduling separate from trading logic.
    - Orchestrator remains single-cycle and testable.
    """

    def __init__(self, orchestrator: Orchestrator, config: SchedulerConfig) -> None:
        self._orchestrator: Orchestrator = orchestrator
        self._config: SchedulerConfig = config

    def run_forever(self) -> None:
        """
        Run cycles until the process is interrupted.

        Ctrl+C behaviour
        - KeyboardInterrupt stops the loop cleanly.
        """
        logger.info(f"Scheduler started. cycle_seconds={self._config.cycle_seconds}")

        try:
            while True:
                self._orchestrator.run_cycle()
                time.sleep(self._config.cycle_seconds)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user (KeyboardInterrupt).")
