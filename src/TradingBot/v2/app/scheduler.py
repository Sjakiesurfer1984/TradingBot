from __future__ import annotations

import time
from dataclasses import dataclass

from TradingBot.v2.logger import setup_logger
from TradingBot.v2.orchestrator import OrchestratorV2

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


class SchedulerV2:
    """
    Very small scheduler for running Option C cycles repeatedly.

    Design intent
    - Keep scheduling separate from trading logic.
    - Orchestrator remains single-cycle and testable.
    """

    def __init__(self, orchestrator: OrchestratorV2, config: SchedulerConfig) -> None:
        self._orchestrator: OrchestratorV2 = orchestrator
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
