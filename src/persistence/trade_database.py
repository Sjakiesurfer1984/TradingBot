from __future__ import annotations

"""
src/persistence/trade_database.py
----------------------------------
SQLite-backed implementation of TradeDatabaseABC.

leg_role field (added):
  Every fill and position now carries a leg_role: 'leap' | 'near' | ''.
  This is the source of truth for the state classifier — no more guessing
  from DTE thresholds whether a position is a LEAP or a NEAR.

  _run_migrations() adds the column to existing DBs on first startup.
  Safe to run repeatedly — idempotent.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.persistence.interfaces import TradeDatabaseABC
from src.utilities.logger import setup_logger

logger = setup_logger("TradeDatabase")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action          TEXT NOT NULL,
    fill_date       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    leg_role        TEXT DEFAULT '',
    expiry          TEXT,
    strike          REAL,
    option_right    TEXT,
    qty             INTEGER NOT NULL,
    fill_price      REAL NOT NULL,
    cost_basis_usd  REAL NOT NULL,
    brokerage_fee   REAL DEFAULT 0.0,
    usd_aud_rate    REAL,
    cost_basis_aud  REAL,
    notes           TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fills_date       ON fills(fill_date);
CREATE INDEX IF NOT EXISTS idx_fills_symbol     ON fills(symbol);
CREATE INDEX IF NOT EXISTS idx_fills_underlying ON fills(underlying);

CREATE TABLE IF NOT EXISTS positions (
    symbol              TEXT PRIMARY KEY,
    underlying          TEXT NOT NULL,
    side                TEXT NOT NULL,
    qty                 REAL NOT NULL,
    leg_role            TEXT DEFAULT '',
    open_date           TEXT NOT NULL,
    cost_basis_usd      REAL NOT NULL,
    market_value_usd    REAL DEFAULT 0.0,
    last_updated        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chain_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, contract_symbol)
);

CREATE INDEX IF NOT EXISTS idx_chain_date_ul ON chain_snapshots(snapshot_date, underlying);

CREATE TABLE IF NOT EXISTS equity_curve (
    snapshot_date        TEXT PRIMARY KEY,
    equity               REAL NOT NULL,
    cash                 REAL NOT NULL,
    option_buying_power  REAL NOT NULL,
    open_positions       INTEGER NOT NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Migrations for existing DBs. Each is idempotent — fails silently if column exists.
_MIGRATIONS = [
    "ALTER TABLE fills     ADD COLUMN leg_role TEXT DEFAULT ''",
    "ALTER TABLE positions ADD COLUMN leg_role TEXT DEFAULT ''",
]


@dataclass
class TradeDatabase(TradeDatabaseABC):
    db_path: Path

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._run_migrations()
        logger.info("TradeDatabase initialised | path=%s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _run_migrations(self) -> None:
        """Add new columns to existing DBs. Safe to run on every startup."""
        with self._connect() as conn:
            for sql in _MIGRATIONS:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record_fill(
        self,
        *,
        action:          str,
        symbol:          str,
        underlying:      str,
        qty:             int,
        fill_price:      float,
        fill_date:       date,
        expiry:          Optional[date],
        strike:          Optional[float],
        option_right:    Optional[str],
        cost_basis_usd:  float,
        leg_role:        str = "",
        brokerage_fee:   float = 0.0,
        usd_aud_rate:    Optional[float] = None,
        notes:           str = "",
    ) -> int:
        cost_basis_aud = (
            round(cost_basis_usd / usd_aud_rate, 2)
            if usd_aud_rate and usd_aud_rate > 0 else None
        )
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO fills (
                    action, fill_date, symbol, underlying, leg_role,
                    expiry, strike, option_right,
                    qty, fill_price, cost_basis_usd, brokerage_fee,
                    usd_aud_rate, cost_basis_aud, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.upper(), fill_date.isoformat(), symbol.upper(), underlying.upper(),
                    leg_role.lower(),
                    expiry.isoformat() if expiry else None,
                    strike, option_right.upper() if option_right else None,
                    qty, fill_price, cost_basis_usd, brokerage_fee,
                    usd_aud_rate, cost_basis_aud, notes,
                ),
            )
            row_id = cur.lastrowid

        logger.info(
            "Fill recorded | id=%d action=%s symbol=%s role=%s qty=%d price=%.2f cost_usd=%.2f",
            row_id, action, symbol, leg_role or "?", qty, fill_price, cost_basis_usd,
        )
        self._sync_position(
            action=action, symbol=symbol, underlying=underlying,
            qty=qty, fill_price=fill_price, fill_date=fill_date,
            cost_basis_usd=cost_basis_usd, leg_role=leg_role,
        )
        return row_id

    def _sync_position(
        self,
        *,
        action:         str,
        symbol:         str,
        underlying:     str,
        qty:            int,
        fill_price:     float,
        fill_date:      date,
        cost_basis_usd: float,
        leg_role:       str = "",
    ) -> None:
        action = action.upper()
        sym    = symbol.upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE symbol = ?", (sym,)
            ).fetchone()

            if action in ("BTO", "STO"):
                side     = "long" if action == "BTO" else "short"
                new_role = leg_role.lower()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO positions
                            (symbol, underlying, side, qty, leg_role, open_date,
                             cost_basis_usd, market_value_usd)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (sym, underlying.upper(), side, float(qty),
                         new_role, fill_date.isoformat(), cost_basis_usd, 0.0),
                    )
                else:
                    new_qty  = float(row["qty"]) + (qty if action == "BTO" else -qty)
                    # Only overwrite role if we have one — don't blank out an existing role
                    stored_role = new_role or (row["leg_role"] or "")
                    conn.execute(
                        "UPDATE positions SET qty=?, leg_role=?, "
                        "cost_basis_usd=cost_basis_usd+?, last_updated=datetime('now') "
                        "WHERE symbol=?",
                        (new_qty, stored_role, cost_basis_usd, sym),
                    )

            elif action in ("BTC", "STC"):
                if row is not None:
                    new_qty = float(row["qty"]) - qty
                    if abs(new_qty) < 0.001:
                        conn.execute("DELETE FROM positions WHERE symbol=?", (sym,))
                    else:
                        conn.execute(
                            "UPDATE positions SET qty=?, last_updated=datetime('now') WHERE symbol=?",
                            (new_qty, sym),
                        )

    def record_chain_snapshot(
        self,
        *,
        snapshot_date: date,
        underlying:    str,
        contracts:     List[Dict[str, Any]],
    ) -> None:
        date_str   = snapshot_date.isoformat()
        underlying = underlying.upper()
        rows = [
            (date_str, underlying, str(c.get("contract_symbol", "")).upper(), json.dumps(c))
            for c in contracts if c.get("contract_symbol")
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO chain_snapshots "
                "(snapshot_date, underlying, contract_symbol, snapshot_json) VALUES (?, ?, ?, ?)",
                rows,
            )
        logger.info(
            "Chain snapshot recorded | date=%s underlying=%s contracts=%d",
            date_str, underlying, len(rows),
        )

    def record_equity_snapshot(
        self,
        *,
        snapshot_date:       date,
        equity:              float,
        cash:                float,
        option_buying_power: float,
        open_positions:      int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity_curve "
                "(snapshot_date, equity, cash, option_buying_power, open_positions) "
                "VALUES (?, ?, ?, ?, ?)",
                (snapshot_date.isoformat(), equity, cash, option_buying_power, open_positions),
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_position_roles(self, underlying: str) -> Dict[str, str]:
        """
        Return {osi_symbol: leg_role} for all open positions on this underlying
        where leg_role is known ('leap' or 'near').

        The state classifier calls this first. Positions not in the result
        (role='') fall back to DTE heuristics in the classifier.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, leg_role FROM positions "
                "WHERE underlying=? AND leg_role != ''",
                (underlying.upper(),),
            ).fetchall()
        return {row["symbol"]: row["leg_role"] for row in rows}

    def get_fills(
        self,
        *,
        from_date:  Optional[date] = None,
        to_date:    Optional[date] = None,
        underlying: Optional[str]  = None,
        action:     Optional[str]  = None,
    ) -> List[Dict[str, Any]]:
        query  = "SELECT * FROM fills WHERE 1=1"
        params = []
        if from_date:
            query += " AND fill_date >= ?"; params.append(from_date.isoformat())
        if to_date:
            query += " AND fill_date <= ?"; params.append(to_date.isoformat())
        if underlying:
            query += " AND underlying = ?"; params.append(underlying.upper())
        if action:
            query += " AND action = ?"; params.append(action.upper())
        query += " ORDER BY fill_date, id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_equity_curve(
        self,
        *,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        query  = "SELECT * FROM equity_curve WHERE 1=1"
        params = []
        if from_date:
            query += " AND snapshot_date >= ?"; params.append(from_date.isoformat())
        if to_date:
            query += " AND snapshot_date <= ?"; params.append(to_date.isoformat())
        query += " ORDER BY snapshot_date"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_chain_snapshot(
        self,
        *,
        snapshot_date: date,
        underlying:    str,
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_json FROM chain_snapshots WHERE snapshot_date=? AND underlying=?",
                (snapshot_date.isoformat(), underlying.upper()),
            ).fetchall()
        return [json.loads(r["snapshot_json"]) for r in rows]