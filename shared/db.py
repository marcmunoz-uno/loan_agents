"""
shared/db.py — SQLite database layer.

Uses raw SQL strings (no ORM) so the schema is trivially portable to MySQL.
All public helpers return dicts or lists of dicts — no sqlite3.Row leakage.

Usage:
    from shared.db import get_conn, init_db, fetchone, fetchall, execute

    with get_conn() as conn:
        row = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?", (prequal_id,))
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional


DB_PATH = os.environ.get("DB_PATH", "data/dealflow.db")
# Seconds a connection waits on a locked DB before raising, via PRAGMA
# busy_timeout (the same mechanism sqlite3.connect(timeout=) uses).
BUSY_TIMEOUT_S = float(os.environ.get("DB_BUSY_TIMEOUT_S", "5"))
MIGRATION_PATH = Path(__file__).parent / "migrations" / "001_initial.sql"
MIGRATION_002_PATH = Path(__file__).parent / "migrations" / "002_loan_processor.sql"
MIGRATION_003_PATH = Path(__file__).parent / "migrations" / "003_intake.sql"
MIGRATION_004_PATH = Path(__file__).parent / "migrations" / "004_prequal_letters.sql"
MIGRATION_005_PATH = Path(__file__).parent / "migrations" / "005_typeform_intake.sql"
MIGRATION_006_PATH = Path(__file__).parent / "migrations" / "006_tx_coordinator_v2.sql"
MIGRATION_007_PATH = Path(__file__).parent / "migrations" / "007_tx_guardrails.sql"


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_conn() -> sqlite3.Connection:
    """Return an open sqlite3 connection with row_factory set to dict."""
    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Without a busy timeout, a write that collides with another connection's
    # write (gunicorn runs multiple workers/threads + the tx sweeper) fails
    # immediately with "database is locked" instead of waiting. WAL allows
    # concurrent readers + a single writer; this makes the writer queue.
    # (Same mechanism as sqlite3.connect(timeout=); set via PRAGMA to be visible.)
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
    return conn


def init_db() -> None:
    """Run all migrations in order. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    migrations = [
        MIGRATION_PATH, MIGRATION_002_PATH, MIGRATION_003_PATH,
        MIGRATION_004_PATH, MIGRATION_005_PATH, MIGRATION_006_PATH,
        MIGRATION_007_PATH,
    ]
    with get_conn() as conn:
        for path in migrations:
            if not path.exists():
                raise FileNotFoundError(f"Migration file not found: {path}")
            sql = path.read_text()
            conn.executescript(sql)
        _apply_schema_patches(conn)
        conn.commit()


def _apply_schema_patches(conn: sqlite3.Connection) -> None:
    """
    In-place ALTER TABLE patches for columns added to existing tables.

    The .sql migration files are the source-of-truth for *fresh* DBs; this
    function is what makes the schema converge for DBs that were created
    before a column was added (e.g. Render's persistent disk between deploys).
    Idempotent — checks PRAGMA table_info before each ALTER.
    """
    additions = {
        "prequal_letters": [
            ("pdf_url",            "TEXT DEFAULT ''"),
            ("pdf_url_expires_at", "TEXT DEFAULT ''"),
        ],
        "intake_documents": [
            ("source_message_id",  "TEXT DEFAULT ''"),
            ("source",             "TEXT DEFAULT ''"),
        ],
        "loan_borrower_intakes": [
            ("letter_id",              "TEXT DEFAULT ''"),
            ("liquid_assets_computed", "REAL"),
        ],
        # ── Transaction Coordinator (merged from ai_transaction_coordinator) ──
        "transactions": [
            # Day 0 of the milestone timeline. NULL means "use created_at as Day 0".
            ("psa_execution_date", "TEXT"),
            # Per-tx override of the global TX_AGENT_MODE. NULL = follow env var.
            ("agent_mode",         "TEXT"),
            # Arive loan record ID — FK for every Arive action on this tx.
            ("arive_loan_id",      "TEXT DEFAULT ''"),
        ],
        "tx_parties": [
            # 'arive' rows are synced from the LOS; 'agent' rows added via the
            # API or by Sam during chat. Sync never deletes 'agent' rows.
            ("source",            "TEXT DEFAULT 'agent'"),
            # Stable Arive contact id so sync can upsert without duplicating.
            ("arive_contact_id",  "TEXT DEFAULT ''"),
            # ISO timestamp of the last successful Arive sync that touched this row.
            ("synced_at",         "TEXT DEFAULT ''"),
        ],
    }
    for table, cols in additions.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue  # table doesn't exist yet — earlier migration handles it
        for col_name, col_def in cols:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


def fetchone(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict]:
    row = conn.execute(sql, params).fetchone()
    return row  # already a dict via row_factory


def fetchall(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return conn.execute(sql, params).fetchall()


def execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Execute a write statement and return the cursor (for lastrowid etc.)."""
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def insert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> int:
    """INSERT a dict into table. Returns the new row's id."""
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    cur = execute(conn, sql, tuple(data.values()))
    return cur.lastrowid


def update(conn: sqlite3.Connection, table: str, data: dict[str, Any],
           where: str, where_params: tuple = ()) -> int:
    """UPDATE rows matching `where`. Returns rowcount."""
    set_clause = ", ".join(f"{k} = ?" for k in data)
    sql = f"UPDATE {table} SET {set_clause} WHERE {where}"
    cur = execute(conn, sql, tuple(data.values()) + where_params)
    return cur.rowcount
