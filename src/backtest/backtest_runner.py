from __future__ import annotations

"""
src/backtest/backtest_runner.py
--------------------------------
Steps through a date range calling orchestrator.run_cycle() once per trading day.

Clock injection:
  The runner owns a BacktestClock and advances it each cycle. The broker and
  snapshot builder both depend on ClockABC — they automatically see the correct
  historical date. No subclassing, no snapshot mutation, no monkey-patching.
"""

import csv
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import List

from src.backtest.alpaca_historical_broker import AlpacaHistoricalBroker
from src.orchestration.orchestrator import TradingOrchestrator
from src.orchestration.orchestrator_interface import CycleRunResult
from src.utilities.clock import BacktestClock
from src.utilities.logger import setup_logger

logger = setup_logger("BacktestRunner")

_HOLIDAYS: set = {
    # 2023
    date(2023, 1, 2),  date(2023, 1, 16), date(2023, 2, 20), date(2023, 4, 7),
    date(2023, 5, 29), date(2023, 6, 19), date(2023, 7, 4),  date(2023, 9, 4),
    date(2023, 11, 23),date(2023, 12, 25),
    # 2024
    date(2024, 1, 1),  date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4),  date(2024, 9, 2),
    date(2024, 11, 28),date(2024, 12, 25),
    # 2025
    date(2025, 1, 1),  date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),  date(2025, 9, 1),
    date(2025, 11, 27),date(2025, 12, 25),
    # 2026
    date(2026, 1, 1),  date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),  date(2026, 9, 7),
    date(2026, 11, 26),date(2026, 12, 25),
}


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def _trading_days(start: date, end: date) -> List[date]:
    days, current = [], start
    while current <= end:
        if _is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


@dataclass
class DayResult:
    date:                str
    cycle_result:        CycleRunResult
    equity:              float
    cash:                float
    option_buying_power: float
    open_positions:      int


def _progress(current: int, total: int, width: int = 40) -> str:
    filled = int(width * current / total) if total else 0
    bar    = "█" * filled + "░" * (width - filled)
    pct    = 100 * current / total if total else 0
    return f"\r[{bar}] {pct:5.1f}%  {current}/{total}"


@dataclass
class BacktestRunner:
    orchestrator: TradingOrchestrator
    broker:       AlpacaHistoricalBroker
    clock:        BacktestClock
    start_date:   date
    end_date:     date
    output_dir:   Path = field(default_factory=lambda: Path("backtest_results"))
    log_mode:     str  = "progress"

    _day_results: List[DayResult] = field(default_factory=list, init=False)

    def run(self) -> List[DayResult]:
        days  = _trading_days(self.start_date, self.end_date)
        total = len(days)

        logger.info(
            "Backtest starting | from=%s to=%s trading_days=%d",
            self.start_date, self.end_date, total,
        )

        _silenced: List[logging.Logger] = []
        if self.log_mode == "progress":
            _noisy = [
                "CycleSnapshotBuilder", "PmccStateClassifier", "PmccStrategy",
                "PmccContractSelector", "AlpacaHistoricalBroker", "TradingOrchestrator",
                "RiskEngine", "BrokerBase", "SimulatedAccount", "IntentEvaluators",
                "DefaultExecutionPolicy",
            ]
            for name in _noisy:
                lg = logging.getLogger(name)
                lg.setLevel(logging.WARNING)
                _silenced.append(lg)

        try:
            for i, d in enumerate(days):
                self.clock.set_date(d)
                self.broker.set_date(d)

                try:
                    result = self.orchestrator.run_cycle()
                except Exception:
                    if self.log_mode == "verbose":
                        logger.exception("Cycle failed | date=%s", d.isoformat())
                    result = CycleRunResult(
                        orders_submitted=0, intents_generated=0,
                        intents_approved=0, intents_rejected=0,
                    )

                self.broker.update_position_market_values()

                summary    = self.broker.account.summary(d.isoformat())
                day_result = DayResult(
                    date=d.isoformat(),
                    cycle_result=result,
                    equity=summary["equity"],
                    cash=summary["cash"],
                    option_buying_power=summary["option_buying_power"],
                    open_positions=summary["open_positions"],
                )
                self._day_results.append(day_result)

                if self.log_mode == "verbose":
                    logger.info(
                        "Day complete | date=%s equity=%.2f cash=%.2f "
                        "positions=%d submitted=%d approved=%d rejected=%d",
                        d.isoformat(), day_result.equity, day_result.cash,
                        day_result.open_positions, result.orders_submitted,
                        result.intents_approved, result.intents_rejected,
                    )
                else:
                    sys.stdout.write(
                        _progress(i + 1, total)
                        + f"  equity=${day_result.equity:,.0f}"
                        f"  pos={day_result.open_positions}"
                    )
                    sys.stdout.flush()

        finally:
            for lg in _silenced:
                lg.setLevel(logging.DEBUG)

        if self.log_mode == "progress":
            sys.stdout.write("\n")
            sys.stdout.flush()

        self._write_results()
        self._print_summary()
        return self._day_results

    def _write_results(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

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
                    "cash":                r.cash,
                    "option_buying_power": r.option_buying_power,
                    "open_positions":      r.open_positions,
                    "orders_submitted":    r.cycle_result.orders_submitted,
                    "intents_approved":    r.cycle_result.intents_approved,
                    "intents_rejected":    r.cycle_result.intents_rejected,
                })
        logger.info("Equity curve written | path=%s", csv_path)

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
        ann_return   = total_return * (252 / days) if days > 0 else 0.0

        peak, max_dd = first.equity, 0.0
        for r in self._day_results:
            peak   = max(peak, r.equity)
            max_dd = max(max_dd, (peak - r.equity) / peak * 100)

        print("\n" + "=" * 60)
        print("  BACKTEST SUMMARY")
        print(f"  Period:        {first.date} → {last.date}  ({days} days)")
        print(f"  Start equity:  ${first.equity:>12,.2f}")
        print(f"  End equity:    ${last.equity:>12,.2f}")
        print(f"  Total return:  {total_return:+.2f}%")
        print(f"  Ann. return:   {ann_return:+.2f}%")
        print(f"  Max drawdown:  {max_dd:.2f}%")
        print(f"  Total trades:  {trades}")
        print("=" * 60)