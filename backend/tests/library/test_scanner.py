from __future__ import annotations

import os
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector


def _pdf(marker: bytes) -> bytes:
    return b"%PDF-1.7\n" + marker + b"\n%%EOF"


@pytest.fixture
def scanner_env(tmp_path: Path) -> tuple[LibraryScanner, Database, LibraryRepository, TaskQueue]:
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    inspector = SafeWindowsFileInspector()
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    repository = LibraryRepository(database)
    return LibraryScanner(database, repository, inspector, queue), database, repository, queue


def _register(repository: LibraryRepository, root: Path, *, recursive: bool = True):
    inspector = SafeWindowsFileInspector()
    return repository.register(
        inspector.inspect_root_directory(root),
        recursive=recursive,
        authorization_source="native_picker_confirmed",
    )


def test_scan_deduplicates_pdf_content_and_records_non_pdf_metadata_only(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    (root / "a.pdf").write_bytes(_pdf(b"same"))
    (root / "duplicate.pdf").write_bytes(_pdf(b"same"))
    (root / "different.pdf").write_bytes(_pdf(b"different"))
    (root / "notes.docx").write_bytes(b"secret non-pdf bytes")
    library = _register(repository, root)

    summary = scanner.scan_library(library.id)

    assert summary.pdf_paths == 3
    assert summary.unique_contents == 2
    assert summary.duplicate_groups == 1
    assert summary.unsupported_paths == 1
    assert len(summary.queued_content_ids) == 2
    assert queue.count(kind="parse_pdf") == 2
    with database.connect() as connection:
        unsupported = connection.execute(
            "SELECT * FROM documents WHERE display_name = 'notes.docx'"
        ).fetchone()
        pdfs = connection.execute(
            "SELECT * FROM documents WHERE format = 'pdf' ORDER BY display_name"
        ).fetchall()
        contents = connection.execute("SELECT * FROM contents").fetchall()
    assert unsupported is not None
    assert unsupported["content_sha256"] is None
    assert unsupported["status"] == "unsupported_v01"
    assert unsupported["authorization_source"] == "unsupported_format"
    assert unsupported["auto_cloud_eligible"] == 0
    assert all(row["status"] == "candidate_pdf" for row in pdfs)
    assert all(row["auto_cloud_eligible"] == 0 for row in pdfs)
    assert all(row["authorization_source"] == "registered_library" for row in pdfs)
    assert {row["status"] for row in contents} == {"candidate_pdf"}


def test_rename_preserves_document_id_but_hard_link_creates_separate_unauthorized_path(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    old = root / "old.pdf"
    old.write_bytes(_pdf(b"rename"))
    library = _register(repository, root)
    scanner.scan_library(library.id)
    with database.connect() as connection:
        original_id = connection.execute("SELECT id FROM documents").fetchone()["id"]

    renamed = root / "renamed.pdf"
    old.rename(renamed)
    scanner.scan_library(library.id)
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == original_id
    assert rows[0]["display_name"] == "renamed.pdf"

    linked = root / "linked.pdf"
    os.link(renamed, linked)
    scanner.scan_library(library.id)
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, auto_cloud_eligible, hard_link_count FROM documents ORDER BY display_name"
        ).fetchall()
    assert len(rows) == 2
    assert original_id in {row["id"] for row in rows}
    assert all(UUID(row["id"]) for row in rows)
    assert all(row["hard_link_count"] == 2 for row in rows)
    assert all(row["auto_cloud_eligible"] == 0 for row in rows)


def test_modification_versions_content_and_deletion_marks_missing_without_losing_history(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "paper.pdf"
    paper.write_bytes(_pdf(b"v1"))
    library = _register(repository, root)
    scanner.scan_library(library.id)

    paper.write_bytes(_pdf(b"version-two-is-longer"))
    scanner.scan_library(library.id)
    paper.unlink()
    scanner.scan_library(library.id)

    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
        versions = connection.execute(
            "SELECT * FROM document_versions WHERE document_id = ?", (document["id"],)
        ).fetchall()
    assert document["status"] == "missing"
    assert document["auto_cloud_eligible"] == 0
    assert len(versions) == 2
    assert len({row["content_sha256"] for row in versions}) == 2


def test_non_recursive_scan_ignores_nested_files_and_reparse_entries(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "direct.pdf").write_bytes(_pdf(b"direct"))
    (nested / "nested.pdf").write_bytes(_pdf(b"nested"))
    library = _register(repository, root, recursive=False)

    summary = scanner.scan_library(library.id)

    assert summary.pdf_paths == 1
    with database.connect() as connection:
        names = {row["display_name"] for row in connection.execute("SELECT * FROM documents")}
    assert names == {"direct.pdf"}


def test_scan_counts_and_never_follows_reparse_escape(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "escaped.pdf").write_bytes(_pdf(b"must-not-be-read"))
    escape = root / "escape"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except OSError as error:  # pragma: no cover - Windows policy dependent
        pytest.skip(f"directory symlink unavailable: {error.winerror}")
    library = _register(repository, root)

    summary = scanner.scan_library(library.id)

    assert summary.pdf_paths == 0
    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        persisted = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (f"library_scan_status:{library.id}",),
        ).fetchone()
    assert json.loads(persisted["value_json"])["warnings"] == ["unsafe_paths_skipped"]
    assert json.loads(persisted["value_json"])["unsafe_paths"] == 1


def test_queue_failure_rolls_back_content_document_and_job_together(tmp_path: Path) -> None:
    class FailingQueue(TaskQueue):
        def enqueue_in_transaction(self, *args, **kwargs):
            raise RuntimeError("injected_queue_failure")

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    inspector = SafeWindowsFileInspector()
    repository = LibraryRepository(database)
    scanner = LibraryScanner(
        database,
        repository,
        inspector,
        FailingQueue(database, lambda: datetime.now(UTC)),
    )
    root = tmp_path / "library"
    root.mkdir()
    (root / "paper.pdf").write_bytes(_pdf(b"transaction"))
    library = _register(repository, root)

    with pytest.raises(RuntimeError, match="injected_queue_failure"):
        scanner.scan_library(library.id)

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_managed_import_root_never_creates_scanner_documents(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, _repository, queue = scanner_env
    root = tmp_path / "managed"
    root.mkdir()
    (root / "manual.pdf").write_bytes(_pdf(b"manual"))
    identity = SafeWindowsFileInspector().inspect_root_directory(root)
    library_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO libraries (
              id, root_path, root_path_key, root_volume_id, root_file_id, recursive,
              discovery_mode, cloud_pdf_auto_allowed, authorization_source, status,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 'managed_import', 1,
                      'managed_import_internal', 'active', ?, ?)
            """,
            (
                str(library_id),
                str(identity.canonical_path),
                os.path.normcase(os.path.abspath(identity.canonical_path)),
                identity.volume_id,
                identity.file_id,
                now,
                now,
            ),
        )

    summary = scanner.scan_library(library_id)

    assert summary.pdf_paths == 0
    assert queue.count(kind="parse_pdf") == 0
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
