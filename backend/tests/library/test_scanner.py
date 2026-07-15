from __future__ import annotations

import os
import json
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread, current_thread
from uuid import UUID, uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.coordinator import LibraryScanCoordinator
from literature_workbench.features.library.scanner import LibraryScanner
import literature_workbench.features.library.scanner as scanner_module
from literature_workbench.platform.windows.filesystem import (
    FileIdentity,
    RootIdentity,
    SafeWindowsFileInspector,
    UnsafePath,
    VerifiedHandle,
)
from io import BytesIO


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


def test_unchanged_validated_single_link_pdf_preserves_task8_cloud_state(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "validated.pdf"
    paper.write_bytes(_pdf(b"validated"))
    library = _register(repository, root)
    scanner.scan_library(library.id)
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE documents
            SET status = 'valid_pdf', auto_cloud_eligible = 1,
                authorization_source = 'registered_library'
            """
        )
        connection.execute("UPDATE contents SET status = 'valid_pdf'")

    scanner.scan_library(library.id)

    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
        content = connection.execute("SELECT * FROM contents").fetchone()
    assert document["status"] == "valid_pdf"
    assert document["auto_cloud_eligible"] == 1
    assert document["authorization_source"] == "registered_library"
    assert content["status"] == "valid_pdf"


@pytest.mark.parametrize("stale_status", ["missing", "incomplete"])
def test_successful_rediscovery_recovers_pdf_and_non_pdf_status(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
    stale_status: str,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    (root / "paper.pdf").write_bytes(_pdf(b"same"))
    (root / "notes.docx").write_bytes(b"metadata-only")
    library = _register(repository, root)
    scanner.scan_library(library.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET status = ?, auto_cloud_eligible = 0", (stale_status,)
        )

    scanner.scan_library(library.id)

    with database.connect() as connection:
        rows = {
            row["display_name"]: row
            for row in connection.execute("SELECT * FROM documents").fetchall()
        }
    assert rows["paper.pdf"]["status"] == "candidate_pdf"
    assert rows["paper.pdf"]["auto_cloud_eligible"] == 0
    assert rows["notes.docx"]["status"] == "unsupported_v01"
    assert rows["notes.docx"]["auto_cloud_eligible"] == 0


def test_last_root_check_failure_discards_every_discovered_candidate(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "existing.pdf").write_bytes(_pdf(b"existing"))
    delegate = SafeWindowsFileInspector()
    root_identity = delegate.inspect_root_directory(root)
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    baseline = LibraryScanner(
        database, repository, delegate, TaskQueue(database, lambda: datetime.now(UTC))
    )
    baseline.scan_library(library.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET status = 'valid_pdf', auto_cloud_eligible = 1"
        )
    (root / "new.pdf").write_bytes(_pdf(b"new"))
    checks = 0

    class LastCheckFails:
        def __getattr__(self, name: str):
            return getattr(delegate, name)

        def inspect_root_directory(self, candidate: Path) -> RootIdentity:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise UnsafePath("final_root_identity_failed")
            return delegate.inspect_root_directory(candidate)

    scanner = LibraryScanner(
        database,
        repository,
        LastCheckFails(),
        TaskQueue(database, lambda: datetime.now(UTC)),
    )

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths >= 1
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "existing.pdf"
    assert rows[0]["status"] == "incomplete"
    assert rows[0]["auto_cloud_eligible"] == 0


def test_incremental_last_root_check_failure_discards_batch_and_invalidates_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    existing = root / "existing.pdf"
    existing.write_bytes(_pdf(b"existing"))
    delegate = SafeWindowsFileInspector()
    root_identity = delegate.inspect_root_directory(root)
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    LibraryScanner(database, repository, delegate, queue).scan_library(library.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET status = 'valid_pdf', auto_cloud_eligible = 1"
        )
    added = root / "added.pdf"
    added.write_bytes(_pdf(b"added"))
    checks = 0

    class LastIncrementalCheckFails:
        def __getattr__(self, name: str):
            return getattr(delegate, name)

        def inspect_root_directory(self, candidate: Path) -> RootIdentity:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise UnsafePath("final_incremental_root_identity_failed")
            return delegate.inspect_root_directory(candidate)

    scanner = LibraryScanner(
        database,
        repository,
        LastIncrementalCheckFails(),
        queue,
    )

    summary = scanner.reconcile_paths(library.id, (existing, added))

    assert summary.unsafe_paths >= 1
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "existing.pdf"
    assert rows[0]["status"] == "incomplete"
    assert rows[0]["auto_cloud_eligible"] == 0


def test_incremental_scan_status_remains_full_library_aggregate(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    paths = [root / f"{index}.pdf" for index in range(3)]
    for index, path in enumerate(paths):
        path.write_bytes(_pdf(str(index).encode()))
    library = _register(repository, root)
    scanner.scan_library(library.id)
    paths[0].write_bytes(_pdf(b"changed"))

    scanner.reconcile_path(library.id, paths[0])

    with database.connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (f"library_scan_status:{library.id}",),
        ).fetchone()
    status = json.loads(row["value_json"])
    assert status["pdf_paths"] == 3
    assert status["unique_contents"] == 3
    assert status["duplicate_groups"] == 0


def test_full_and_incremental_scans_share_one_snapshot_lock(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "paper.pdf"
    paper.write_bytes(_pdf(b"old"))
    delegate = SafeWindowsFileInspector()
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    library = repository.register(
        delegate.inspect_root_directory(root),
        recursive=True,
        authorization_source="native_picker_confirmed",
    )
    coordinator = LibraryScanCoordinator()
    old_reconcile_reached = Event()
    release_old = Event()
    new_started = Event()
    new_root_check = Event()
    errors: list[BaseException] = []

    class SignalingInspector:
        def __getattr__(self, name: str):
            return getattr(delegate, name)

        def inspect_root_directory(self, candidate: Path) -> RootIdentity:
            if current_thread().name == "scan-new":
                new_root_check.set()
            return delegate.inspect_root_directory(candidate)

    class PausingScanner(LibraryScanner):
        def _reconcile(self, *args, **kwargs):
            old_reconcile_reached.set()
            if not release_old.wait(5):
                raise TimeoutError("old_scan_not_released")
            return super()._reconcile(*args, **kwargs)

    inspector = SignalingInspector()
    old_scanner = PausingScanner(
        database, repository, inspector, queue, coordinator=coordinator
    )
    new_scanner = LibraryScanner(
        database, repository, inspector, queue, coordinator=coordinator
    )

    def run_old() -> None:
        try:
            old_scanner.scan_library(library.id)
        except BaseException as error:
            errors.append(error)

    def run_new() -> None:
        new_started.set()
        try:
            new_scanner.reconcile_path(library.id, paper)
        except BaseException as error:
            errors.append(error)

    old_thread = Thread(target=run_old, name="scan-old")
    new_thread = Thread(target=run_new, name="scan-new")
    old_thread.start()
    assert old_reconcile_reached.wait(2)
    paper.write_bytes(_pdf(b"new"))
    expected_stat = paper.stat()
    expected_hash = hashlib.sha256(paper.read_bytes()).hexdigest()
    new_thread.start()
    assert new_started.wait(1)
    overtook_old_scan = new_root_check.wait(0.5)
    release_old.set()
    old_thread.join(5)
    new_thread.join(5)

    assert not overtook_old_scan
    assert not old_thread.is_alive()
    assert not new_thread.is_alive()
    assert errors == []
    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
    assert document["content_sha256"] == expected_hash
    assert document["size_bytes"] == expected_stat.st_size
    assert document["mtime_ns"] == expected_stat.st_mtime_ns


def test_deleted_hardlink_refreshes_active_peer_and_never_resurrects_missing_history(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    a = root / "a.pdf"
    b = root / "b.pdf"
    c = root / "c.pdf"
    a.write_bytes(_pdf(b"hardlink"))
    os.link(a, b)
    library = _register(repository, root)
    scanner.scan_library(library.id)

    b.unlink()
    scanner.reconcile_path(library.id, b)
    with database.connect() as connection:
        rows = {
            row["display_name"]: row
            for row in connection.execute("SELECT * FROM documents").fetchall()
        }
    assert rows["b.pdf"]["status"] == "missing"
    assert rows["a.pdf"]["hard_link_count"] == 1
    assert rows["a.pdf"]["status"] == "candidate_pdf"
    assert rows["a.pdf"]["auto_cloud_eligible"] == 0

    os.link(a, c)
    scanner.reconcile_path(library.id, c)
    with database.connect() as connection:
        rows = {
            row["display_name"]: row
            for row in connection.execute("SELECT * FROM documents").fetchall()
        }
    assert rows["b.pdf"]["status"] == "missing"
    assert rows["b.pdf"]["hard_link_count"] == 2
    assert rows["a.pdf"]["hard_link_count"] == 2
    assert rows["c.pdf"]["hard_link_count"] == 2
    assert rows["a.pdf"]["status"] == "candidate_pdf"
    assert rows["c.pdf"]["status"] == "candidate_pdf"


def test_scandir_runs_only_while_directory_chain_is_held(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    root_identity = SafeWindowsFileInspector().inspect_root_directory(root)
    held = False
    scandir_calls = 0

    class HoldingInspector:
        def inspect_root_directory(self, _candidate: Path) -> RootIdentity:
            return root_identity

        @contextmanager
        def hold_directory_for_enumeration(self, _candidate: Path, _root):
            nonlocal held
            held = True
            try:
                yield
            finally:
                held = False

        def inspect_under_root(self, _candidate: Path, _root):
            raise AssertionError("empty library")

        def inspect_regular_file(self, _candidate: Path, _root):
            raise AssertionError("empty library")

        def open_registered_pdf(self, _document):
            raise AssertionError("empty library")

    class EmptyEntries:
        def __enter__(self):
            nonlocal scandir_calls
            scandir_calls += 1
            assert held, "scandir must not run after verification handles close"
            return iter(())

        def __exit__(self, *_args: object) -> None:
            return None

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    scanner = LibraryScanner(
        database,
        repository,
        HoldingInspector(),
        TaskQueue(database, lambda: datetime.now(UTC)),
    )
    original_scandir = scanner_module.os.scandir
    scanner_module.os.scandir = lambda _path: EmptyEntries()
    try:
        scanner.scan_library(library.id)
    finally:
        scanner_module.os.scandir = original_scandir

    assert scandir_calls == 1


def test_real_held_directory_chain_blocks_swap_before_scandir(tmp_path: Path) -> None:
    root = tmp_path / "library"
    nested = root / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    (nested / "inside.pdf").write_bytes(_pdf(b"inside"))
    (outside / "outside.pdf").write_bytes(_pdf(b"outside"))
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    inspector = SafeWindowsFileInspector()
    repository = LibraryRepository(database)
    library = repository.register(
        inspector.inspect_root_directory(root),
        recursive=True,
        authorization_source="native_picker_confirmed",
    )
    scanner = LibraryScanner(
        database,
        repository,
        inspector,
        TaskQueue(database, lambda: datetime.now(UTC)),
    )
    original_scandir = scanner_module.os.scandir
    race_attempted = False
    swap_succeeded = False
    parked = root / "parked"

    def racing_scandir(path: Path):
        nonlocal race_attempted, swap_succeeded
        if Path(path) == nested and not race_attempted:
            race_attempted = True
            try:
                nested.rename(parked)
            except OSError:
                pass
            else:  # pragma: no cover - would expose a Windows sharing-mode regression
                swap_succeeded = True
                parked.rename(nested)
        return original_scandir(path)

    scanner_module.os.scandir = racing_scandir
    try:
        scanner.scan_library(library.id)
    finally:
        scanner_module.os.scandir = original_scandir

    assert race_attempted
    assert not swap_succeeded
    with database.connect() as connection:
        names = {
            row["display_name"] for row in connection.execute("SELECT * FROM documents")
        }
    assert names == {"inside.pdf"}


def test_incremental_reconcile_hashes_only_changed_paths(tmp_path: Path) -> None:
    class CountingInspector:
        def __init__(self) -> None:
            self.delegate = SafeWindowsFileInspector()
            self.opened: list[Path] = []

        def inspect_root_directory(self, candidate: Path):
            return self.delegate.inspect_root_directory(candidate)

        def inspect_under_root(self, candidate: Path, root):
            return self.delegate.inspect_under_root(candidate, root)

        def inspect_regular_file(self, candidate: Path, root):
            return self.delegate.inspect_regular_file(candidate, root)

        def hold_directory_for_enumeration(self, candidate: Path, root):
            return self.delegate.hold_directory_for_enumeration(candidate, root)

        def open_registered_pdf(self, document):
            self.opened.append(document.path)
            return self.delegate.open_registered_pdf(document)

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    repository = LibraryRepository(database)
    inspector = CountingInspector()
    scanner = LibraryScanner(database, repository, inspector, queue)
    root = tmp_path / "library"
    root.mkdir()
    paths = [root / f"{name}.pdf" for name in ("a", "b", "untouched")]
    for index, path in enumerate(paths):
        path.write_bytes(_pdf(str(index).encode()))
    library = repository.register(
        inspector.inspect_root_directory(root),
        recursive=True,
        authorization_source="native_picker_confirmed",
    )
    scanner.scan_library(library.id)
    inspector.opened.clear()
    paths[0].write_bytes(_pdf(b"changed-a"))
    paths[1].write_bytes(_pdf(b"changed-b"))

    scanner.reconcile_paths(library.id, paths[:2])

    assert {path.name for path in inspector.opened} == {"a.pdf", "b.pdf"}
    assert all(path.name != "untouched.pdf" for path in inspector.opened)


def test_incremental_rename_hard_link_and_delete_use_minimal_affected_scope(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    old = root / "old.pdf"
    old.write_bytes(_pdf(b"identity"))
    library = _register(repository, root)
    scanner.scan_library(library.id)
    with database.connect() as connection:
        original_id = connection.execute("SELECT id FROM documents").fetchone()[0]

    renamed = root / "renamed.pdf"
    old.rename(renamed)
    scanner.reconcile_paths(library.id, (old, renamed))
    with database.connect() as connection:
        renamed_row = connection.execute(
            "SELECT * FROM documents WHERE display_name = 'renamed.pdf'"
        ).fetchone()
    assert renamed_row["id"] == original_id
    with database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET status = 'valid_pdf', auto_cloud_eligible = 1"
        )
        connection.execute("UPDATE contents SET status = 'valid_pdf'")

    linked = root / "linked.pdf"
    os.link(renamed, linked)
    scanner.reconcile_path(library.id, linked)
    with database.connect() as connection:
        active = connection.execute(
            "SELECT * FROM documents WHERE status <> 'missing' ORDER BY display_name"
        ).fetchall()
    assert [row["display_name"] for row in active] == ["linked.pdf", "renamed.pdf"]
    assert len({row["id"] for row in active}) == 2
    assert all(row["auto_cloud_eligible"] == 0 for row in active)
    assert all(row["status"] == "candidate_pdf" for row in active)

    linked.unlink()
    scanner.reconcile_path(library.id, linked)
    with database.connect() as connection:
        linked_row = connection.execute(
            "SELECT * FROM documents WHERE display_name = 'linked.pdf'"
        ).fetchone()
        renamed_row = connection.execute(
            "SELECT * FROM documents WHERE display_name = 'renamed.pdf'"
        ).fetchone()
    assert linked_row["status"] == "missing"
    assert renamed_row["status"] != "missing"


def test_persisted_pdf_metadata_comes_from_stably_hashed_verified_handle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "paper.pdf"
    payload = _pdf(b"actual-handle")
    paper.write_bytes(payload)
    root_identity = SafeWindowsFileInspector().inspect_root_directory(root)
    library_id = uuid4()
    seed = FileIdentity(paper, library_id, "volume", "file", 1, 10, 1, False)
    actual = FileIdentity(
        paper, library_id, "volume", "file", len(payload), 99, 1, False
    )
    inspections = iter((seed, actual))

    class StableHandleInspector:
        def inspect_root_directory(self, _candidate: Path) -> RootIdentity:
            return root_identity

        def inspect_under_root(self, _candidate: Path, _root):
            return next(inspections)

        def inspect_regular_file(self, _candidate: Path, _root):
            return actual

        @contextmanager
        def hold_directory_for_enumeration(self, _candidate: Path, _root):
            yield root_identity

        def open_registered_pdf(self, _document):
            return VerifiedHandle(actual, BytesIO(payload))

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    scanner = LibraryScanner(
        database,
        repository,
        StableHandleInspector(),
        TaskQueue(database, lambda: datetime.now(UTC)),
    )

    scanner.scan_library(library.id)

    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
        version = connection.execute("SELECT * FROM document_versions").fetchone()
    assert document["size_bytes"] == len(payload)
    assert document["mtime_ns"] == 99
    assert version["size_bytes"] == len(payload)
    assert version["mtime_ns"] == 99


def test_hash_change_between_verified_reads_is_incomplete_and_persists_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "paper.pdf"
    paper.write_bytes(_pdf(b"enumerated"))
    root_identity = SafeWindowsFileInspector().inspect_root_directory(root)
    library_id = uuid4()
    first_payload = _pdf(b"first")
    second_payload = _pdf(b"second")
    identity = FileIdentity(
        paper, library_id, "volume", "file", len(first_payload), 10, 1, False
    )
    payloads = iter((first_payload, second_payload))

    class ChangingHandleInspector:
        def inspect_root_directory(self, _candidate: Path) -> RootIdentity:
            return root_identity

        def inspect_under_root(self, _candidate: Path, _root):
            return identity

        def inspect_regular_file(self, _candidate: Path, _root):
            return identity

        @contextmanager
        def hold_directory_for_enumeration(self, _candidate: Path, _root):
            yield root_identity

        def open_registered_pdf(self, _document):
            return VerifiedHandle(identity, BytesIO(next(payloads)))

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    scanner = LibraryScanner(
        database,
        repository,
        ChangingHandleInspector(),
        TaskQueue(database, lambda: datetime.now(UTC)),
    )

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        persisted = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (f"library_scan_status:{library.id}",),
        ).fetchone()
    assert "incomplete_scan" in json.loads(persisted["value_json"])["warnings"]


def test_verified_metadata_change_during_hash_is_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "paper.pdf"
    payload = _pdf(b"same-bytes")
    paper.write_bytes(payload)
    root_identity = SafeWindowsFileInspector().inspect_root_directory(root)
    library_id = uuid4()
    before = FileIdentity(
        paper, library_id, "volume", "file", len(payload), 10, 1, False
    )
    during = FileIdentity(
        paper, library_id, "volume", "file", len(payload), 11, 1, False
    )
    identities = iter((before, during))

    class MetadataChangingInspector:
        def inspect_root_directory(self, _candidate: Path) -> RootIdentity:
            return root_identity

        def inspect_under_root(self, _candidate: Path, _root):
            return before

        def inspect_regular_file(self, _candidate: Path, _root):
            return during

        @contextmanager
        def hold_directory_for_enumeration(self, _candidate: Path, _root):
            yield root_identity

        def open_registered_pdf(self, _document):
            return VerifiedHandle(next(identities), BytesIO(payload))

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    scanner = LibraryScanner(
        database,
        repository,
        MetadataChangingInspector(),
        TaskQueue(database, lambda: datetime.now(UTC)),
    )

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


@pytest.mark.parametrize("swap", ["root_reparse_swap", "intermediate_reparse_swap"])
def test_non_pdf_revalidation_rejects_root_or_intermediate_swap(
    tmp_path: Path,
    swap: str,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "metadata.docx").write_bytes(b"must-not-be-opened")
    root_identity = SafeWindowsFileInspector().inspect_root_directory(root)

    class SwappingInspector:
        def inspect_root_directory(self, _candidate: Path) -> RootIdentity:
            return root_identity

        def inspect_under_root(self, _candidate: Path, _root):
            raise AssertionError("PDF inspection not expected")

        def inspect_regular_file(self, _candidate: Path, _root):
            raise UnsafePath(swap)

        @contextmanager
        def hold_directory_for_enumeration(self, _candidate: Path, _root):
            yield root_identity

        def open_registered_pdf(self, _document):
            raise AssertionError("PDF open not expected")

    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(database)
    library = repository.register(
        root_identity, recursive=True, authorization_source="native_picker_confirmed"
    )
    scanner = LibraryScanner(
        database,
        repository,
        SwappingInspector(),
        TaskQueue(database, lambda: datetime.now(UTC)),
    )

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        status = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (f"library_scan_status:{library.id}",),
        ).fetchone()
    assert "incomplete_scan" in json.loads(status["value_json"])["warnings"]


def test_entry_stat_error_is_incomplete_and_does_not_mark_known_document_missing(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "known.pdf"
    paper.write_bytes(_pdf(b"known"))
    library = _register(repository, root)
    scanner.scan_library(library.id)

    class FailingEntry:
        def stat(self, *, follow_symlinks: bool):
            assert follow_symlinks is False
            raise OSError("injected stat failure")

    class Entries:
        def __enter__(self):
            return iter((FailingEntry(),))

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(scanner_module.os, "scandir", lambda _path: Entries())

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
        status = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (f"library_scan_status:{library.id}",),
        ).fetchone()
    assert document["status"] == "candidate_pdf"
    assert "incomplete_scan" in json.loads(status["value_json"])["warnings"]


def test_incremental_stat_error_marks_only_affected_document_incomplete(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner, database, repository, _queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    affected = root / "affected.pdf"
    unaffected = root / "unaffected.pdf"
    affected.write_bytes(_pdf(b"affected"))
    unaffected.write_bytes(_pdf(b"unaffected"))
    library = _register(repository, root)
    scanner.scan_library(library.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET status = 'valid_pdf', auto_cloud_eligible = 1"
        )
        connection.execute("UPDATE contents SET status = 'valid_pdf'")
    original_lstat = scanner_module.os.lstat

    def failing_lstat(path: Path):
        if Path(path) == affected:
            raise OSError("injected incremental stat failure")
        return original_lstat(path)

    monkeypatch.setattr(scanner_module.os, "lstat", failing_lstat)

    summary = scanner.reconcile_path(library.id, affected)

    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        rows = {
            row["display_name"]: row
            for row in connection.execute("SELECT * FROM documents").fetchall()
        }
    assert rows["affected.pdf"]["status"] == "incomplete"
    assert rows["affected.pdf"]["auto_cloud_eligible"] == 0
    assert rows["unaffected.pdf"]["status"] == "valid_pdf"
    assert rows["unaffected.pdf"]["auto_cloud_eligible"] == 1


def test_root_replacement_after_enumeration_is_incomplete_and_preserves_known_rows(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_scanner, database, repository, queue = scanner_env
    root = tmp_path / "library"
    root.mkdir()
    paper = root / "known.pdf"
    paper.write_bytes(_pdf(b"known"))
    library = _register(repository, root)
    original_scanner.scan_library(library.id)
    root_identity = SafeWindowsFileInspector().inspect_root_directory(root)
    checks = 0

    class RootSwappingInspector:
        def inspect_root_directory(self, _candidate: Path) -> RootIdentity:
            nonlocal checks
            checks += 1
            if checks == 1:
                return root_identity
            raise UnsafePath("root_replaced_after_enumeration")

        def inspect_under_root(self, _candidate: Path, _root):
            raise AssertionError("empty enumeration must inspect no files")

        def inspect_regular_file(self, _candidate: Path, _root):
            raise AssertionError("empty enumeration must inspect no files")

        @contextmanager
        def hold_directory_for_enumeration(self, _candidate: Path, _root):
            yield root_identity

        def open_registered_pdf(self, _document):
            raise AssertionError("empty enumeration must open no files")

    class EmptyEntries:
        def __enter__(self):
            return iter(())

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(scanner_module.os, "scandir", lambda _path: EmptyEntries())
    scanner = LibraryScanner(database, repository, RootSwappingInspector(), queue)

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths >= 1
    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
    assert document["status"] == "incomplete"
    assert document["auto_cloud_eligible"] == 0


def test_intermediate_replacement_after_empty_enumeration_preserves_known_rows(
    scanner_env: tuple[LibraryScanner, Database, LibraryRepository, TaskQueue],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_scanner, database, repository, queue = scanner_env
    root = tmp_path / "library"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "known.pdf").write_bytes(_pdf(b"known"))
    library = _register(repository, root)
    original_scanner.scan_library(library.id)
    delegate = SafeWindowsFileInspector()

    class IntermediateSwappingInspector:
        def inspect_root_directory(self, candidate: Path) -> RootIdentity:
            return delegate.inspect_root_directory(candidate)

        @contextmanager
        def hold_directory_for_enumeration(self, candidate: Path, _root):
            if candidate == nested:
                raise UnsafePath("intermediate_replaced_after_enumeration")
            yield delegate.inspect_root_directory(root)

        def inspect_under_root(self, _candidate: Path, _root):
            raise AssertionError("empty enumeration must inspect no files")

        def inspect_regular_file(self, _candidate: Path, _root):
            raise AssertionError("empty enumeration must inspect no files")

        def open_registered_pdf(self, _document):
            raise AssertionError("empty enumeration must open no files")

    class NestedEntry:
        path = str(nested)

        def stat(self, *, follow_symlinks: bool):
            return os.stat(nested, follow_symlinks=follow_symlinks)

    class Entries:
        def __init__(self, values: tuple[object, ...]) -> None:
            self.values = values

        def __enter__(self):
            return iter(self.values)

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        scanner_module.os,
        "scandir",
        lambda path: Entries((NestedEntry(),)) if Path(path) == root else Entries(()),
    )
    scanner = LibraryScanner(database, repository, IntermediateSwappingInspector(), queue)

    summary = scanner.scan_library(library.id)

    assert summary.unsafe_paths == 1
    with database.connect() as connection:
        document = connection.execute("SELECT * FROM documents").fetchone()
    assert document["status"] == "incomplete"
    assert document["auto_cloud_eligible"] == 0


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
    assert json.loads(persisted["value_json"])["warnings"] == [
        "unsafe_paths_skipped",
        "incomplete_scan",
    ]
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
