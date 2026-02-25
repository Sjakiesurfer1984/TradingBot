from __future__ import annotations

"""
src/persistence/ato_report.py
-------------------------------
Generates ATO-compliant CGT reports from the TradeDatabase.

Australian Tax Office requirements for options (CGT Event D2 / D1):

  OPENING (STO — writing a call):
    CGT Event D2 applies. The premium received is a capital gain in the
    income year the option is written. If the option is later exercised
    or closed, an amendment is required. If it expires worthless, the
    gain is confirmed.

  CLOSING (BTC — buying back the short, or STC — selling the long):
    CGT Event A1 (disposal of a CGT asset). Proceeds minus cost base.

  HOLDING PERIOD:
    Options held > 12 months qualify for the 50% CGT discount (individuals
    and trusts only). Practically rare for short-dated options; more relevant
    for LEAP positions.

  FOREIGN CURRENCY (s.960-50 ITAA 1997):
    All amounts must be converted to AUD at the exchange rate on the date
    of each transaction. Store usd_aud_rate in the fills table.

  RECORDS:
    Must be kept for 5 years after the CGT event (s.121-25 ITAA 1997).

Report columns (ATO Schedule 3 style):
    fill_date, action, symbol, underlying, expiry, strike, right,
    qty, fill_price_usd, cost_basis_usd, usd_aud_rate, cost_basis_aud,
    brokerage_fee_usd, brokerage_fee_aud,
    open_date (if this is a close), open_cost_basis_aud,
    proceeds_aud, gain_loss_aud, held_days, cgt_discount_eligible,
    notes

Usage:
    python -m src.persistence.ato_report \\
        --db data/trades.db \\
        --fy 2025 \\
        --out reports/
"""

import csv
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.persistence.interfaces import TradeDatabaseABC
from src.utilities.logger import setup_logger

logger = setup_logger("AtoReport")


def _fy_date_range(fy_ending: int) -> tuple[date, date]:
    """Return (start, end) for the Australian financial year ending 30 June fy_ending."""
    return date(fy_ending - 1, 7, 1), date(fy_ending, 6, 30)


@dataclass
class AtoReportGenerator:
    """
    Generates ATO Schedule 3 / CGT event reports from trade fill data.

    Matching method: FIFO (first-in first-out) within each symbol.
    This is the default under s.115-30 ITAA 1997 when no specific
    identification is made.

    SOLID:
      - SRP: only generates reports. Persistence is TradeDatabase.
      - DIP: depends on TradeDatabaseABC — works with any implementation.
    """

    db: TradeDatabaseABC

    def generate_fy_report(
        self,
        *,
        fy_ending:  int,
        output_dir: Path,
        underlying: Optional[str] = None,
    ) -> Path:
        """
        Generate a CGT report for the Australian financial year ending 30 June fy_ending.
        Returns the path of the written CSV.
        """
        from_date, to_date = _fy_date_range(fy_ending)
        suffix   = f"_{underlying.upper()}" if underlying else ""
        out_path = output_dir / f"ato_cgt_{fy_ending - 1}_{fy_ending}{suffix}.csv"

        output_dir.mkdir(parents=True, exist_ok=True)

        fills = self.db.get_fills(
            from_date=from_date, to_date=to_date, underlying=underlying
        )
        rows = self._build_report_rows(fills)
        self._write_csv(rows, out_path)

        logger.info(
            "ATO report written | fy=%d/%d rows=%d path=%s",
            fy_ending - 1, fy_ending, len(rows), out_path,
        )
        return out_path

    def generate_full_report(
        self,
        *,
        output_dir: Path,
        underlying: Optional[str] = None,
    ) -> Path:
        """Generate a report covering all fills in the database."""
        suffix   = f"_{underlying.upper()}" if underlying else ""
        out_path = output_dir / f"ato_cgt_all_time{suffix}.csv"

        output_dir.mkdir(parents=True, exist_ok=True)

        fills = self.db.get_fills(underlying=underlying)
        rows  = self._build_report_rows(fills)
        self._write_csv(rows, out_path)

        logger.info("ATO full report written | rows=%d path=%s", len(rows), out_path)
        return out_path

    # ------------------------------------------------------------------
    # Core matching logic
    # ------------------------------------------------------------------

    def _build_report_rows(
        self,
        fills: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Match opening fills to closing fills (FIFO) and produce CGT event rows.

        STO (writing an option): CGT Event D2 — premium is a capital gain
        when written. One D2 row per STO fill.

        BTC / STC (closing): CGT Event A1 — disposal. Matched against earliest
        unmatched open. One A1 row per matched pair.
        """
        rows: List[Dict[str, Any]] = []

        opens_by_symbol:  Dict[str, List[Dict]] = {}
        closes_by_symbol: Dict[str, List[Dict]] = {}

        for f in fills:
            sym = f["symbol"]
            act = f["action"].upper()
            if act in ("BTO", "STO"):
                opens_by_symbol.setdefault(sym, []).append(dict(f))
            elif act in ("BTC", "STC"):
                closes_by_symbol.setdefault(sym, []).append(dict(f))

        all_symbols = set(list(opens_by_symbol) + list(closes_by_symbol))

        for sym in sorted(all_symbols):
            opens  = list(opens_by_symbol.get(sym, []))
            closes = list(closes_by_symbol.get(sym, []))

            # D2 event for every STO open
            for op in opens:
                if op["action"].upper() == "STO":
                    rows.append(self._make_d2_row(op))

            # A1 disposal for every close — FIFO matched
            open_queue = list(opens)
            for op in open_queue:
                op["_remaining_qty"] = int(op["qty"])

            for cl in closes:
                remaining = int(cl["qty"])

                while remaining > 0 and open_queue:
                    op      = open_queue[0]
                    op_qty  = op["_remaining_qty"]
                    matched = min(remaining, op_qty)
                    remaining           -= matched
                    op["_remaining_qty"] -= matched

                    if op["_remaining_qty"] <= 0:
                        open_queue.pop(0)

                    rows.append(
                        self._make_disposal_row(
                            open_fill=op,
                            close_fill=cl,
                            matched_qty=matched,
                        )
                    )

                if remaining > 0:
                    rows.append(self._make_unmatched_close_row(cl, remaining))

        rows.sort(key=lambda r: r.get("event_date") or "")
        return rows

    # ------------------------------------------------------------------
    # Row constructors
    # ------------------------------------------------------------------

    def _make_d2_row(self, fill: Dict[str, Any]) -> Dict[str, Any]:
        """CGT Event D2 — writing an option (STO)."""
        aud_rate = fill.get("usd_aud_rate")
        cost_usd = abs(float(fill.get("cost_basis_usd") or 0))
        cost_aud = round(cost_usd / aud_rate, 2) if aud_rate else None
        missing  = "MISSING — add usd_aud_rate before lodging"

        return {
            "event_date":              fill["fill_date"],
            "cgt_event":               "D2 — option grant (STO)",
            "action":                  "STO",
            "symbol":                  fill["symbol"],
            "underlying":              fill["underlying"],
            "expiry":                  fill.get("expiry") or "",
            "strike":                  fill.get("strike") or "",
            "option_right":            fill.get("option_right") or "",
            "qty":                     fill["qty"],
            "fill_price_usd":          fill["fill_price"],
            "cost_basis_usd":          cost_usd,
            "usd_aud_rate":            aud_rate or missing,
            "proceeds_or_premium_aud": cost_aud or missing,
            "brokerage_fee_usd":       fill.get("brokerage_fee") or 0.0,
            "open_date":               fill["fill_date"],
            "close_date":              "",
            "open_cost_basis_aud":     cost_aud or "",
            "capital_gain_aud":        cost_aud or missing,
            "capital_loss_aud":        "",
            "held_days":               "",
            "cgt_50pct_discount":      "No",
            "notes":                   fill.get("notes") or "",
        }

    def _make_disposal_row(
        self,
        *,
        open_fill:   Dict[str, Any],
        close_fill:  Dict[str, Any],
        matched_qty: int,
    ) -> Dict[str, Any]:
        """CGT Event A1 — disposal (BTC or STC)."""
        open_date  = date.fromisoformat(open_fill["fill_date"])
        close_date = date.fromisoformat(close_fill["fill_date"])
        held_days  = (close_date - open_date).days

        # Pro-rata cost if partial fill matched
        open_total = int(open_fill["qty"])
        pro_rata   = matched_qty / open_total if open_total else 1.0

        open_cost_usd  = abs(float(open_fill.get("cost_basis_usd")  or 0)) * pro_rata
        close_cost_usd = abs(float(close_fill.get("cost_basis_usd") or 0)) * pro_rata

        open_rate  = open_fill.get("usd_aud_rate")
        close_rate = close_fill.get("usd_aud_rate")

        open_cost_aud  = round(open_cost_usd  / open_rate,  2) if open_rate  else None
        close_cost_aud = round(close_cost_usd / close_rate, 2) if close_rate else None

        is_short = open_fill["action"].upper() == "STO"

        # Gain = premium received (open) minus cost to close (for short)
        # Gain = proceeds (close) minus cost paid (open) for long
        if open_cost_aud is not None and close_cost_aud is not None:
            gain_aud = round(
                (open_cost_aud - close_cost_aud) if is_short
                else (close_cost_aud - open_cost_aud),
                2,
            )
        else:
            gain_aud = None

        capital_gain = gain_aud if (gain_aud is not None and gain_aud >= 0) else ""
        capital_loss = abs(gain_aud) if (gain_aud is not None and gain_aud < 0) else ""

        cgt_discount = (
            "N/A (loss)" if (gain_aud is not None and gain_aud < 0)
            else ("Yes" if held_days >= 365 else "No")
        )

        missing = "MISSING — add usd_aud_rate before lodging"

        return {
            "event_date":              close_fill["fill_date"],
            "cgt_event":               "A1 — disposal",
            "action":                  close_fill["action"],
            "symbol":                  close_fill["symbol"],
            "underlying":              close_fill["underlying"],
            "expiry":                  close_fill.get("expiry") or "",
            "strike":                  close_fill.get("strike") or "",
            "option_right":            close_fill.get("option_right") or "",
            "qty":                     matched_qty,
            "fill_price_usd":          close_fill["fill_price"],
            "cost_basis_usd":          round(close_cost_usd, 2),
            "usd_aud_rate":            close_rate or missing,
            "proceeds_or_premium_aud": close_cost_aud or missing,
            "brokerage_fee_usd":       close_fill.get("brokerage_fee") or 0.0,
            "open_date":               open_fill["fill_date"],
            "close_date":              close_fill["fill_date"],
            "open_cost_basis_aud":     open_cost_aud or missing,
            "capital_gain_aud":        capital_gain if gain_aud is not None else missing,
            "capital_loss_aud":        capital_loss,
            "held_days":               held_days,
            "cgt_50pct_discount":      cgt_discount,
            "notes":                   close_fill.get("notes") or "",
        }

    def _make_unmatched_close_row(
        self,
        fill: Dict[str, Any],
        qty:  int,
    ) -> Dict[str, Any]:
        """A close with no matching open — flag for manual review."""
        return {
            "event_date":              fill["fill_date"],
            "cgt_event":               "A1 — REVIEW: no matching open found",
            "action":                  fill["action"],
            "symbol":                  fill["symbol"],
            "underlying":              fill["underlying"],
            "expiry":                  fill.get("expiry") or "",
            "strike":                  fill.get("strike") or "",
            "option_right":            fill.get("option_right") or "",
            "qty":                     qty,
            "fill_price_usd":          fill["fill_price"],
            "cost_basis_usd":          abs(float(fill.get("cost_basis_usd") or 0)),
            "usd_aud_rate":            fill.get("usd_aud_rate") or "MISSING",
            "proceeds_or_premium_aud": "MISSING",
            "brokerage_fee_usd":       fill.get("brokerage_fee") or 0.0,
            "open_date":               "UNKNOWN — may be prior tax year",
            "close_date":              fill["fill_date"],
            "open_cost_basis_aud":     "UNKNOWN",
            "capital_gain_aud":        "REVIEW",
            "capital_loss_aud":        "REVIEW",
            "held_days":               "UNKNOWN",
            "cgt_50pct_discount":      "UNKNOWN",
            "notes": (
                "No matching open fill in database. "
                "If position was opened in a prior financial year, "
                "calculate cost basis from that year's records. "
                + (fill.get("notes") or "")
            ),
        }

    # ------------------------------------------------------------------
    # CSV output
    # ------------------------------------------------------------------

    _COLUMNS = [
        "event_date", "cgt_event", "action", "symbol", "underlying",
        "expiry", "strike", "option_right", "qty",
        "fill_price_usd", "cost_basis_usd",
        "usd_aud_rate", "proceeds_or_premium_aud",
        "brokerage_fee_usd",
        "open_date", "close_date",
        "open_cost_basis_aud",
        "capital_gain_aud", "capital_loss_aud",
        "held_days", "cgt_50pct_discount",
        "notes",
    ]

    def _write_csv(self, rows: List[Dict[str, Any]], path: Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=self._COLUMNS, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)


# ---------------------------------------------------------------------------
# CLI entry point:  python -m src.persistence.ato_report --help
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate ATO CGT report from the PMCC trade database."
    )
    parser.add_argument("--db",  required=True,       help="Path to trades.db SQLite file")
    parser.add_argument("--out", default="reports",   help="Output directory for CSV")
    parser.add_argument(
        "--fy", type=int, default=None,
        help="Financial year ending June 30 (e.g. 2025 = FY2024/25). Omit for all-time.",
    )
    parser.add_argument(
        "--underlying", default=None,
        help="Filter by underlying symbol, e.g. SPY",
    )
    args = parser.parse_args()

    from src.persistence.trade_database import TradeDatabase
    db  = TradeDatabase(Path(args.db))
    gen = AtoReportGenerator(db=db)
    out = Path(args.out)

    if args.fy:
        path = gen.generate_fy_report(
            fy_ending=args.fy, output_dir=out, underlying=args.underlying
        )
    else:
        path = gen.generate_full_report(output_dir=out, underlying=args.underlying)

    print(f"\nReport written → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())