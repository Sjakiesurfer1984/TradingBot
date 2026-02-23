from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import List

from src.backtest.alpaca_historical_broker import AlpacaHistoricalBroker
from src.orchestration.orchestrator import TradingOrchestrator
from src.orchestration.orchestrator_interface import CycleRunResult
from src.utilities.logger import setup_logger


"""
src/backtest/backtest_runner.py
--------------------------------
Steps through a date range, calling orchestrator.run_cycle() once per
trading day. Collects per-cycle results and produces a final summary.

The runner owns the date iteration and hands off to the orchestrator
exactly as the live Scheduler does — the orchestrator never knows it's
in a backtest. This is the point: we test the real code, not a replica.

Usage:
    See run_backtest.py for the entry point.
"""



logger = setup_logger("BacktestRunner")

# US market holidays (update annually).
# A minimal set — any date in here is skipped.
_HOLIDAYS: set = {
    # 2023
    date(2023, 1, 2), date(2023, 1, 16), date(2023, 2, 20), date(2023, 4, 7),
    date(2023, 5, 29), date(2023, 6, 19), date(2023, 7, 4), date(2023, 9, 4),
    date(2023, 11, 23), date(2023, 12, 25),
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
}


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def _trading_days(start: date, end: date) -> List[date]:
    days = []
    current = start
    while current <= end:
        if _is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


@dataclass
class DayResult:
    date:               str
    cycle_result:       CycleRunResult
    equity:             float
    cash:               float
    option_buying_power: float
    open_positions:     int


@dataclass
class BacktestRunner:
    orchestrator:  TradingOrchestrator
    broker:        AlpacaHistoricalBroker
    start_date:    date
    end_date:      date
    output_dir:    Path = Path("backtest_results")

    _day_results:  List[DayResult] = field(default_factory=list, init=False)

    def run(self) -> List[DayResult]:
        days = _trading_days(self.start_date, self.end_date)
        logger.info(
            "Backtest starting | from=%s to=%s trading_days=%d",
            self.start_date, self.end_date, len(days),
        )

        for d in days:
            logger.info("--- Cycle date=%s ---", d.isoformat())

            # Point the broker at today's date before the cycle runs.
            self.broker.set_date(d)

            try:
                result = self.orchestrator.run_cycle()
            except Exception:
                logger.exception("Cycle failed | date=%s — continuing", d.isoformat())
                result = CycleRunResult(
                    orders_submitted=0,
                    intents_generated=0,
                    intents_approved=0,
                    intents_rejected=0,
                )

            # Revalue positions at EOD prices now that chain data is cached.
            self.broker.update_position_market_values()

            summary = self.broker.account.summary(d.isoformat())
            day_result = DayResult(
                date=d.isoformat(),
                cycle_result=result,
                equity=summary["equity"],
                cash=summary["cash"],
                option_buying_power=summary["option_buying_power"],
                open_positions=summary["open_positions"],
            )
            self._day_results.append(day_result)

            logger.info(
                "Day complete | date=%s equity=%.2f cash=%.2f positions=%d "
                "submitted=%d approved=%d rejected=%d",
                d.isoformat(),
                day_result.equity, day_result.cash, day_result.open_positions,
                result.orders_submitted, result.intents_approved, result.intents_rejected,
            )

        self._write_results()
        self._print_summary()
        return self._day_results

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_results(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Daily equity curve CSV
        csv_path = self.output_dir / "equity_curve.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "date", "equity", "cash", "option_buying_power",
                "open_positions", "orders_submitted",
                "intents_approved", "intents_rejected",
            ])
            writer.writeheader()
            for r in self._day_results:
                writer.writerow({
                    "date":                r.date,
                    "equity":              r.equity,
                    "cash":               r.cash,
                    "option_buying_power": r.option_buying_power,
                    "open_positions":      r.open_positions,
                    "orders_submitted":    r.cycle_result.orders_submitted,
                    "intents_approved":    r.cycle_result.intents_approved,
                    "intents_rejected":    r.cycle_result.intents_rejected,
                })
        logger.info("Equity curve written | path=%s", csv_path)

        # Trade log JSON
        trade_log_path = self.output_dir / "trade_log.json"
        with open(trade_log_path, "w") as f:
            json.dump(self.broker.account.trade_log(), f, indent=2)
        logger.info("Trade log written | path=%s", trade_log_path)

    def _print_summary(self) -> None:
        if not self._day_results:
            return
        first  = self._day_results[0]
        last   = self._day_results[-1]
        days   = len(self._day_results)
        trades = len(self.broker.account.trade_log())

        total_return = (last.equity - first.equity) / first.equity * 100
        # Annualised return (approximate)
        ann_return = total_return * (252 / days) if days > 0 else 0.0

        # Max drawdown
        peak = first.equity
        max_dd = 0.0
        for r in self._day_results:
            peak = max(peak, r.equity)
            dd   = (peak - r.equity) / peak * 100
            max_dd = max(max_dd, dd)

        logger.info("=" * 60)
        logger.info("BACKTEST SUMMARY")
        logger.info("  Period:          %s → %s (%d days)", first.date, last.date, days)
        logger.info("  Start equity:    $%.2f", first.equity)
        logger.info("  End equity:      $%.2f", last.equity)
        logger.info("  Total return:    %.2f%%", total_return)
        logger.info("  Ann. return:     %.2f%%", ann_return)
        logger.info("  Max drawdown:    %.2f%%", max_dd)
        logger.info("  Total trades:    %d", trades)
        logger.info("=" * 60)
