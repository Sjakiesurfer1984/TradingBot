from __future__ import annotations

import time
from dataclasses import dataclass

from src.orchestration.orchestrator import TradingOrchestrator
from src.utilities.logger import setup_logger

logger = setup_logger("Scheduler")


@dataclass(frozen=True)
class SchedulerConfig:
    cycle_seconds: float = 60.0
    max_cycles:    int   = 0   # 0 = run forever


@dataclass
class Scheduler:
    """
    Runs TradingOrchestrator.run_cycle() on a timer.

    Holds TradingOrchestrator directly — OrchestratorABC removed.
    One orchestrator exists; the ABC earned nothing.
    """
    orchestrator: TradingOrchestrator
    config:       SchedulerConfig

    def run(self) -> None:
        cycle_count = 0
        logger.info("Scheduler started | cycle_seconds=%.1f", self.config.cycle_seconds)

        while True:
            try:
                result = self.orchestrator.run_cycle()
                cycle_count += 1
                logger.info(
                    "Cycle %d complete | submitted=%d approved=%d rejected=%d",
                    cycle_count,
                    result.orders_submitted,
                    result.intents_approved,
                    result.intents_rejected,
                )
            except KeyboardInterrupt:
                logger.info("Scheduler interrupted — shutting down")
                break
            except Exception:
                logger.exception("Unhandled exception in cycle %d", cycle_count + 1)

            if self.config.max_cycles > 0 and cycle_count >= self.config.max_cycles:
                logger.info("Max cycles reached (%d) — stopping", self.config.max_cycles)
                break

            time.sleep(self.config.cycle_seconds)