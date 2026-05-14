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
MIGRATION_PATH = Path(__file__).parent / "migrations" / "001_initial.sql"


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
    return conn


def init_db() -> None:
    """Run the initial migration if tables don't exist yet."""
    if not MIGRATION_PATH.exists():
        raise FileNotFoundError(f"Migration file not found: {MIGRATION_PATH}")
    sql = MIGRATION_PATH.read_text()
    with get_conn() as conn:
        conn.executescript(sql)
        conn.commit()


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
