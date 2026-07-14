from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from literature_workbench.db.database import Database


SCHEMA_V01_PATH = Path(__file__).with_name("schema_v01.sql")
SCHEMA_V01_VERSION = "0001_v01"


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise ValueError("migration contains an incomplete SQL statement")
    return statements


def run_migrations(db: Database) -> None:
    statements = _split_sql_script(SCHEMA_V01_PATH.read_text(encoding="utf-8"))
    with db.transaction() as connection:
        migrations_table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()
        if migrations_table_exists is not None:
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (SCHEMA_V01_VERSION,),
            ).fetchone()
            if applied is not None:
                return

        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_V01_VERSION, datetime.now(UTC).isoformat()),
        )
