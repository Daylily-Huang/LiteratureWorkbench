from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from literature_workbench.db.database import Database


SCHEMA_V01_PATH = Path(__file__).with_name("schema_v01.sql")
SCHEMA_V01_VERSION = "0001_v01"
SCHEMA_V02_PATH = Path(__file__).with_name("schema_v02_import_ownership.sql")
SCHEMA_V02_VERSION = "0002_import_ownership"
SCHEMA_V03_PATH = Path(__file__).with_name("schema_v03_parser_fencing.sql")
SCHEMA_V03_VERSION = "0003_parser_fencing"
SCHEMA_V04_PATH = Path(__file__).with_name("schema_v04_pdf_annotations.sql")
SCHEMA_V04_VERSION = "0004_pdf_annotations"
SCHEMA_V05_PATH = Path(__file__).with_name("schema_v05_deepseek_billing.sql")
SCHEMA_V05_VERSION = "0005_deepseek_billing"
SCHEMA_V06_PATH = Path(__file__).with_name("schema_v06_deepseek_usage_optional.sql")
SCHEMA_V06_VERSION = "0006_deepseek_usage_optional"
SCHEMA_V07_PATH = Path(__file__).with_name("schema_v07_rag_chunks.sql")
SCHEMA_V07_VERSION = "0007_rag_chunks"
SCHEMA_V08_PATH = Path(__file__).with_name("schema_v08_rag_trigram_rects.sql")
SCHEMA_V08_VERSION = "0008_rag_trigram_rects"


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
            (SCHEMA_V03_VERSION, SCHEMA_V03_PATH),
            (SCHEMA_V04_VERSION, SCHEMA_V04_PATH),
            (SCHEMA_V05_VERSION, SCHEMA_V05_PATH),
            (SCHEMA_V06_VERSION, SCHEMA_V06_PATH),
            (SCHEMA_V07_VERSION, SCHEMA_V07_PATH),
            (SCHEMA_V08_VERSION, SCHEMA_V08_PATH),
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
