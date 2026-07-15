from __future__ import annotations

import sqlite3
from os import path as os_path
from pathlib import Path

import pytest

from literature_workbench.db import migrations
from literature_workbench.db.database import Database, canonical_path_values
from literature_workbench.db.migrations import run_migrations


EXPECTED_TABLES = {
    "app_settings",
    "confirmed_imports",
    "contents",
    "document_versions",
    "documents",
    "egress_audit",
    "import_intents",
    "import_promotions",
    "jobs",
    "libraries",
    "model_validation_tasks",
    "model_validations",
    "one_time_egress_grants",
    "pages",
    "parse_runs",
    "schema_migrations",
    "translation_cache",
    "translation_tasks",
}


def _table_names(db: Database) -> set[str]:
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row["name"]) for row in rows}


def _schema_snapshot(db: Database) -> list[tuple[str, str, str]]:
    with db.connect() as connection:
        rows = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    return [(str(row["type"]), str(row["name"]), str(row["sql"])) for row in rows]


def test_migration_creates_exact_v01_tables_and_is_idempotent(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "nested" / "workbench.sqlite3")

    run_migrations(db)
    first_schema = _schema_snapshot(db)
    with db.connect() as connection:
        first_versions = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    run_migrations(db)

    assert _table_names(db) == EXPECTED_TABLES
    assert _schema_snapshot(db) == first_schema
    with db.connect() as connection:
        second_versions = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [tuple(row) for row in first_versions] == [tuple(row) for row in second_versions]
    assert [row["version"] for row in second_versions] == ["0001_v01", "0002_import_ownership"]
    with db.connect() as connection:
        columns = {
            str(row["name"]): (str(row["type"]), row["notnull"], row["dflt_value"])
            for row in connection.execute("PRAGMA table_info(import_promotions)")
        }
    assert columns["expected_volume_id"][0] == "TEXT"
    assert columns["expected_file_id"][0] == "TEXT"
    assert columns["expected_size_bytes"][0] == "INTEGER"
    assert columns["expected_hard_link_count"][0] == "INTEGER"
    assert columns["moved_volume_id"][0] == "TEXT"
    assert columns["moved_file_id"][0] == "TEXT"
    assert columns["moved_size_bytes"][0] == "INTEGER"
    assert columns["moved_hard_link_count"][0] == "INTEGER"
    assert columns["cleanup_authorized"] == ("INTEGER", 1, "0")
    assert columns["ownership_provenance"] == ("TEXT", 1, "'source_claim'")


def test_import_promotion_cleanup_authorization_requires_moved_identity(
    database: Database,
) -> None:
    timestamp = "2026-07-15T08:00:00+00:00"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO import_intents(
              id,session_generation,file_name,expected_size,expected_media_type,
              upload_token_hash,status,expires_at,created_at,updated_at)
            VALUES('intent-a','generation-a','a.pdf',9,'application/pdf',NULL,'failed',?,?,?)
            """,
            (timestamp, timestamp, timestamp),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO import_promotions(
                  content_sha256,owner_intent_id,final_path_key,status,
                  cleanup_authorized,ownership_provenance,updated_at)
                VALUES('sha','intent-a','c:\\a.pdf','failed',1,'handle_move',?)
                """,
                (timestamp,),
            )


def test_existing_v01_database_upgrades_fail_closed_and_remains_idempotent(
    tmp_path: Path,
) -> None:
    db = Database.open(tmp_path / "existing-v01.sqlite3")
    timestamp = "2026-07-15T08:00:00+00:00"
    with db.transaction() as connection:
        for statement in migrations._split_sql_script(
            migrations.SCHEMA_V01_PATH.read_text(encoding="utf-8")
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES('0001_v01',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO import_intents(
              id,session_generation,file_name,expected_size,expected_media_type,
              upload_token_hash,status,expires_at,created_at,updated_at)
            VALUES('legacy-intent','generation-a','legacy.pdf',9,'application/pdf',NULL,
                   'failed',?,?,?)
            """,
            (timestamp, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO import_promotions(
              content_sha256,owner_intent_id,final_path_key,status,updated_at)
            VALUES('legacy-sha','legacy-intent','c:\\legacy.pdf','failed',?)
            """,
            (timestamp,),
        )

    run_migrations(db)
    first_snapshot = _schema_snapshot(db)
    run_migrations(db)

    assert _schema_snapshot(db) == first_snapshot
    with db.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        legacy = connection.execute(
            """
            SELECT cleanup_authorized,ownership_provenance,expected_file_id,moved_file_id
            FROM import_promotions WHERE content_sha256='legacy-sha'
            """
        ).fetchone()
    assert [str(row["version"]) for row in versions] == [
        "0001_v01",
        "0002_import_ownership",
    ]
    assert tuple(legacy) == (0, "source_claim", None, None)


def test_database_connections_enable_wal_foreign_keys_and_busy_timeout(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")

    with db.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.row_factory is sqlite3.Row


def test_migration_rolls_back_all_ddl_and_version_on_bad_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_schema = tmp_path / "bad-schema.sql"
    bad_schema.write_text(
        """
        CREATE TABLE schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        );
        CREATE TABLE migration_probe (id TEXT PRIMARY KEY);
        CREATE TABL broken_statement (id TEXT);
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "SCHEMA_V01_PATH", bad_schema)
    db = Database.open(tmp_path / "bad.sqlite3")

    with pytest.raises(sqlite3.Error):
        run_migrations(db)

    assert _table_names(db) == set()


def test_migration_rejects_an_incomplete_sql_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incomplete_schema = tmp_path / "incomplete-schema.sql"
    incomplete_schema.write_text(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);\n"
        "CREATE TABLE never_complete (id TEXT PRIMARY KEY",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrations, "SCHEMA_V01_PATH", incomplete_schema)
    db = Database.open(tmp_path / "incomplete.sqlite3")

    with pytest.raises(ValueError, match="incomplete SQL"):
        run_migrations(db)

    assert _table_names(db) == set()


def test_foreign_keys_are_enforced(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO documents (
              id, library_id, path, path_key, display_name, format, size_bytes, mtime_ns,
              volume_id, file_id, hard_link_count, parse_preference, auto_cloud_eligible,
              authorization_source, status, created_at, updated_at
            ) VALUES (
              'document-a', 'missing-library', 'C:\\paper.pdf', 'c:\\paper.pdf', 'paper.pdf',
              'pdf', 1, 1, 'volume', 'file', 1, 'parse', 0, 'user', 'active',
              '2026-07-15T08:00:00+00:00', '2026-07-15T08:00:00+00:00'
            )
            """
        )


def _insert_translation_prerequisites(connection: sqlite3.Connection) -> None:
    timestamp = "2026-07-15T08:00:00+00:00"
    connection.execute(
        """
        INSERT INTO libraries (
          id, root_path, root_path_key, root_volume_id, root_file_id, recursive,
          discovery_mode, cloud_pdf_auto_allowed, authorization_source, status,
          created_at, updated_at
        ) VALUES ('library-a', 'C:\\papers', 'c:\\papers', 'volume', 'root-file', 1,
                  'monitored', 0, 'user', 'active', ?, ?)
        """,
        (timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO contents (
          sha256, size_bytes, mime_type, status, created_at, updated_at
        ) VALUES ('sha256-a', 100, 'application/pdf', 'ready', ?, ?)
        """,
        (timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO documents (
          id, library_id, content_sha256, path, path_key, display_name, format, size_bytes,
          mtime_ns, volume_id, file_id, hard_link_count, parse_preference,
          auto_cloud_eligible, authorization_source, status, created_at, updated_at
        ) VALUES ('document-a', 'library-a', 'sha256-a', 'C:\\papers\\a.pdf',
                  'c:\\papers\\a.pdf', 'a.pdf', 'pdf', 100, 1, 'volume', 'file-a', 1,
                  'parse', 0, 'user', 'active', ?, ?)
        """,
        (timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO document_versions (
          id, document_id, content_sha256, size_bytes, mtime_ns, volume_id, file_id,
          hard_link_count, observed_at
        ) VALUES ('version-a', 'document-a', 'sha256-a', 100, 1, 'volume', 'file-a', 1, ?)
        """,
        (timestamp,),
    )


def _insert_translation_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    client_task_id: str,
    route_mode: str,
    validation_id: str | None,
) -> None:
    timestamp = "2026-07-15T08:00:00+00:00"
    connection.execute(
        """
        INSERT INTO translation_tasks (
          id, session_generation, client_task_id, document_id, document_version_id,
          content_sha256, physical_page, selected_text, context_before, context_after,
          target_language, route_mode, requested_provider, requested_model,
          model_validation_id, settings_revision, prompt_version, status, stage,
          created_at, updated_at
        ) VALUES (?, 'generation-a', ?, 'document-a', 'version-a', 'sha256-a', 1,
                  'hello', '', '', 'zh', ?, 'openai', 'model-a', ?, 1, 'v1',
                  'queued', 'pending', ?, ?)
        """,
        (task_id, client_task_id, route_mode, validation_id, timestamp, timestamp),
    )


def test_model_validation_and_translation_reference_round_trip(database: Database) -> None:
    timestamp = "2026-07-15T08:00:00+00:00"
    with database.transaction() as connection:
        _insert_translation_prerequisites(connection)
        connection.execute(
            """
            INSERT INTO model_validations (id, provider, role, model, validated_at)
            VALUES ('validation-a', 'openai', 'strong', 'model-a', ?)
            """,
            (timestamp,),
        )
        _insert_translation_task(
            connection,
            task_id="translation-online",
            client_task_id="client-online",
            route_mode="online",
            validation_id="validation-a",
        )
        _insert_translation_task(
            connection,
            task_id="translation-local",
            client_task_id="client-local",
            route_mode="local_only",
            validation_id=None,
        )

    with database.connect() as connection:
        validation = connection.execute(
            "SELECT id, provider, role, model FROM model_validations WHERE id = 'validation-a'"
        ).fetchone()
        references = connection.execute(
            "SELECT id, model_validation_id FROM translation_tasks ORDER BY id"
        ).fetchall()

    assert validation is not None
    assert tuple(validation) == ("validation-a", "openai", "strong", "model-a")
    assert [tuple(row) for row in references] == [
        ("translation-local", None),
        ("translation-online", "validation-a"),
    ]


def test_translation_task_rejects_unknown_model_validation(database: Database) -> None:
    with database.transaction() as connection:
        _insert_translation_prerequisites(connection)

    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        _insert_translation_task(
            connection,
            task_id="translation-bad",
            client_task_id="client-bad",
            route_mode="online",
            validation_id="missing-validation",
        )


def test_canonical_path_values_keep_display_path_and_normalize_key(tmp_path: Path) -> None:
    source = tmp_path / "MixedCase.PDF"
    source.write_bytes(b"pdf")

    display_path, path_key = canonical_path_values(source)

    resolved = str(source.resolve(strict=True))
    assert display_path == resolved
    assert path_key == os_path.normcase(resolved)
