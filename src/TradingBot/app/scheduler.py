from __future__ import annotations

import time
from dataclasses import dataclass

from TradingBot.orchestration.orchestrator_interface import CycleRunResult, OrchestratorABC
from TradingBot.utilities.logger import setup_logger

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
    Very small scheduler for running cycles repeatedly.

    Design intent
    - Keep scheduling separate from trading logic.
    - Orchestrator remains single-cycle and testable.
    """

    def __init__(self, orchestrator: OrchestratorABC, config: SchedulerConfig) -> None:
        self._orchestrator: OrchestratorABC = orchestrator
        self._config: SchedulerConfig = config

    def run_forever(self) -> None:
        while True:
            result: CycleRunResult = self._orchestrator.run_cycle()

            logger.info(
                "Cycle result | status=%s intents=%d decisions=%d submitted=%d dry_run=%s error=%s",
                result.status.value,
                result.intents_count,
                result.decisions_count,
                result.submitted_count,
                result.dry_run,
                result.error_message,
            )

            time.sleep(self._config.cycle_seconds)
