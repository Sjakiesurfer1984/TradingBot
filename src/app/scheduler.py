from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.app.market_calendar import AlwaysOpenMarketCalendar, MarketCalendarABC
from src.orchestration.orchestrator import TradingOrchestrator
from src.utilities.logger import setup_logger

logger = setup_logger("Scheduler")


@dataclass(frozen=True)
class SchedulerConfig:
    cycle_seconds:       float = 300.0   # normal cadence — no pending orders
    order_check_seconds: float = 30.0    # fast cadence — open order in flight
    max_cycles:          int   = 0       # 0 = run forever


@dataclass
class Scheduler:
    """
    Two-speed scheduler for TradingOrchestrator with market hours guard.

    Market hours guard:
        Before each cycle, the scheduler asks the MarketCalendarABC whether
        the market is open. If it is closed, the scheduler sleeps until the
        next open (reported by the calendar) rather than running a cycle.
        This prevents phantom DB writes and wasted API calls outside trading
        hours — and means it does not matter if the bot is left running
        overnight or over a weekend.

    Normal cadence (cycle_seconds, default 300s / 5 min):
        Used when no orders are in flight.

    Fast cadence (order_check_seconds, default 30s):
        Used when orders are pending. Drops back to normal once clear.
    """

    orchestrator: TradingOrchestrator
    config:       SchedulerConfig
    calendar:     MarketCalendarABC = field(
        default_factory=AlwaysOpenMarketCalendar # default to always-open calendar if not provided, meaning the scheduler will never skip cycles due to market hours. This is a safe default for testing and development, but in production you should provide a real calendar that reflects your market's hours.
    )

    def run(self) -> None:
        cycle_count = 0
        fast_mode   = False

        logger.info(
            "Scheduler started | cycle_seconds=%.0f order_check_seconds=%.0f",
            self.config.cycle_seconds,
            self.config.order_check_seconds,
        )

        while True:
            try:
                # ----------------------------------------------------------
                # Market hours guard — skip cycle entirely if market is closed
                # ----------------------------------------------------------
                if not self.calendar.is_open():
                    wait = self.calendar.seconds_until_open()
                    hours, remainder = divmod(int(wait), 3600)
                    minutes          = remainder // 60
                    logger.info(
                        "Market closed — sleeping %.0fs until next open "
                        "(%dh %02dm)",
                        wait, hours, minutes,
                    )
                    # Sleep in chunks so KeyboardInterrupt is still responsive
                    _interruptible_sleep(wait)
                    continue

                # ----------------------------------------------------------
                # Normal cycle
                # ----------------------------------------------------------
                result       = self.orchestrator.run_cycle()
                cycle_count += 1

                prev_fast = fast_mode
                fast_mode = result.has_pending_orders
                sleep_for = (
                    self.config.order_check_seconds if fast_mode
                    else self.config.cycle_seconds
                )

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

                _interruptible_sleep(sleep_for)

            except KeyboardInterrupt:
                logger.info("Scheduler interrupted — shutting down")
                break
            except Exception:
                logger.exception("Unhandled exception in cycle %d", cycle_count + 1)
                _interruptible_sleep(self.config.cycle_seconds)


def _interruptible_sleep(seconds: float, chunk: float = 5.0) -> None:
    """
    Sleep for `seconds` total, waking every `chunk` seconds.
    This keeps KeyboardInterrupt responsive even during long waits
    (e.g. sleeping until next market open several hours away).
    """
    remaining = seconds
    while remaining > 0:
        time.sleep(min(chunk, remaining))
        remaining -= chunk