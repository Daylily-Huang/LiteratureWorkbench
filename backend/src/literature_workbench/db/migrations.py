from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from literature_workbench.db.database import Database


SCHEMA_V01_PATH = Path(__file__).with_name("schema_v01.sql")
SCHEMA_V01_VERSION = "0001_v01"
SCHEMA_V02_PATH = Path(__file__).with_name("schema_v02_import_ownership.sql")
SCHEMA_V02_VERSION = "0002_import_ownership"


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
    with db.transaction() as connection:
        for version, path in (
            (SCHEMA_V01_VERSION, SCHEMA_V01_PATH),
            (SCHEMA_V02_VERSION, SCHEMA_V02_PATH),
        ):
            migrations_table_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            if migrations_table_exists is not None:
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied is not None:
                    continue
            statements = _split_sql_script(path.read_text(encoding="utf-8"))
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
