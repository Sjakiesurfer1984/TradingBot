from __future__ import annotations

import time
from dataclasses import dataclass

from src.orchestration.orchestrator import TradingOrchestrator
from src.utilities.logger import setup_logger

logger = setup_logger("Scheduler")


@dataclass(frozen=True)
class SchedulerConfig:
    cycle_seconds:       float = 300.0  # normal cadence — no pending orders
    order_check_seconds: float = 30.0   # fast cadence — open order in flight
    max_cycles:          int   = 0      # 0 = run forever


@dataclass
class Scheduler:
    """
    Two-speed scheduler for TradingOrchestrator.

    Normal cadence (cycle_seconds, default 300s / 5 min):
        Used when no orders are in flight. PMCC decisions are driven by
        DTE, IV regime, and delta — all of which move on the timescale
        of hours, not seconds. Polling faster wastes API quota and burns
        through option chain fetch time for no benefit.

    Fast cadence (order_check_seconds, default 30s):
        Used when the previous cycle submitted an order OR the account
        snapshot shows open orders. We want to detect fills quickly so
        the state machine transitions correctly (e.g. PENDING → COVERED).
        Once open_orders is empty the scheduler drops back to normal cadence.

    The speed decision is made from CycleRunResult.has_pending_orders,
    which the orchestrator populates from snapshot.account.open_orders
    plus whether any orders were submitted this cycle.
    """

    orchestrator: TradingOrchestrator
    config:       SchedulerConfig

    def run(self) -> None:
        cycle_count  = 0
        fast_mode    = False

        logger.info(
            "Scheduler started | cycle_seconds=%.0f order_check_seconds=%.0f",
            self.config.cycle_seconds,
            self.config.order_check_seconds,
        )

        while True:
            try:
                result      = self.orchestrator.run_cycle()
                cycle_count += 1

                # Decide next sleep interval based on whether orders are pending
                prev_fast = fast_mode
                fast_mode = result.has_pending_orders
                sleep_for = (
                    self.config.order_check_seconds if fast_mode
                    else self.config.cycle_seconds
                )

                # Log speed transitions so they're visible in logs
                if fast_mode and not prev_fast:
                    logger.info(
                        "Scheduler → FAST mode | open orders detected — "
                        "checking every %.0fs",
                        self.config.order_check_seconds,
                    )
                elif not fast_mode and prev_fast:
                    logger.info(
                        "Scheduler → NORMAL mode | no open orders — "
                        "resuming %.0fs cycle",
                        self.config.cycle_seconds,
                    )

                logger.info(
                    "Cycle %d complete | submitted=%d approved=%d rejected=%d "
                    "pending=%s next_in=%.0fs",
                    cycle_count,
                    result.orders_submitted,
                    result.intents_approved,
                    result.intents_rejected,
                    result.has_pending_orders,
                    sleep_for,
                )

            except KeyboardInterrupt:
                logger.info("Scheduler interrupted — shutting down")
                break
            except Exception:
                logger.exception("Unhandled exception in cycle %d", cycle_count + 1)
                # On unexpected error, use normal cadence — don't hammer the API
                sleep_for = self.config.cycle_seconds

            if self.config.max_cycles > 0 and cycle_count >= self.config.max_cycles:
                logger.info("Max cycles reached (%d) — stopping", self.config.max_cycles)
                break

            time.sleep(sleep_for)