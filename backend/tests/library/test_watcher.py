from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4
from datetime import UTC, datetime

import pytest

from literature_workbench.features.library.models import LibraryRoot, ScanSummary
from literature_workbench.features.library.watcher import WatcherManager
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector


@dataclass(slots=True)
class FakeRepository:
    libraries: dict[UUID, LibraryRoot]

    def get(self, library_id: UUID) -> LibraryRoot:
        return self.libraries[library_id]

    def list_active(self) -> tuple[LibraryRoot, ...]:
        return tuple(self.libraries.values())


@dataclass(slots=True)
class FakeScanner:
    reconciled: list[tuple[UUID, Path]] = field(default_factory=list)
    full_scans: list[UUID] = field(default_factory=list)

    def reconcile_path(self, library_id: UUID, path: Path) -> ScanSummary:
        self.reconciled.append((library_id, path))
        return ScanSummary(0, 0, 0, 0, ())

    def reconcile_paths(
        self, library_id: UUID, paths: tuple[Path, ...]
    ) -> ScanSummary:
        self.reconciled.extend((library_id, path) for path in paths)
        return ScanSummary(0, 0, 0, 0, ())

    def scan_library(self, library_id: UUID) -> ScanSummary:
        self.full_scans.append(library_id)
        return ScanSummary(0, 0, 0, 0, ())


def _library(root: Path, *, recursive: bool = True, mode: str = "monitored") -> LibraryRoot:
    return LibraryRoot(
        id=uuid4(),
        canonical_path=root,
        volume_id="volume",
        file_id="file",
        recursive=recursive,
        discovery_mode=mode,
        authorization_source="native_picker_confirmed",
        status="active",
    )


@pytest.mark.asyncio
async def test_watcher_coalesces_paths_and_discards_outside_before_reconcile(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    library = _library(root)
    scanner = FakeScanner()
    repository = FakeRepository({library.id: library})

    async def no_events(**_kwargs: object) -> AsyncIterator[set[tuple[int, str]]]:
        if False:
            yield set()

    manager = WatcherManager(repository, scanner, watch=no_events)
    inside = root / "paper.pdf"
    outside = tmp_path / "outside.pdf"

    await manager.process_changes(
        library.id,
        {(1, str(inside)), (2, str(inside)), (1, str(outside))},
    )

    assert scanner.reconciled == [(library.id, inside)]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_reconcile_active_starts_only_monitored_watchers_with_recursive_setting(
    tmp_path: Path,
) -> None:
    monitored = _library(tmp_path / "monitored", recursive=False)
    managed = _library(tmp_path / "managed", mode="managed_import")
    calls: list[tuple[Path, bool, int, int]] = []
    started = asyncio.Event()

    async def watch(
        root: Path,
        *,
        recursive: bool,
        debounce: int,
        step: int,
        **_kwargs: object,
    ) -> AsyncIterator[set[tuple[int, str]]]:
        calls.append((root, recursive, debounce, step))
        started.set()
        await asyncio.Future()
        if False:
            yield set()

    manager = WatcherManager(
        FakeRepository({monitored.id: monitored, managed.id: managed}),
        FakeScanner(),
        watch=watch,
    )
    await manager.reconcile_active_libraries()
    await asyncio.wait_for(started.wait(), timeout=1)

    assert manager.active_library_ids == (monitored.id,)
    assert calls == [(monitored.canonical_path, False, 750, 1000)]
    await manager.shutdown()
    assert manager.active_library_ids == ()


@pytest.mark.asyncio
async def test_watcher_restart_schedules_full_rescan(tmp_path: Path) -> None:
    library = _library(tmp_path / "library")
    scanner = FakeScanner()
    attempts = 0
    rescanned = asyncio.Event()

    async def restarting_watch(**_kwargs: object) -> AsyncIterator[set[tuple[int, str]]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("watch root unavailable")
        await asyncio.Future()
        if False:
            yield set()

    class SignallingScanner(FakeScanner):
        def scan_library(self, library_id: UUID) -> ScanSummary:
            result = super().scan_library(library_id)
            rescanned.set()
            return result

    scanner = SignallingScanner()
    manager = WatcherManager(
        FakeRepository({library.id: library}),
        scanner,
        watch=restarting_watch,
        restart_delay=0,
    )
    await manager.add_library(library.id)
    await asyncio.wait_for(rescanned.wait(), timeout=1)

    assert scanner.full_scans == [library.id]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_registered_library_initial_scan_and_later_create_event_are_observed(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    inspector = SafeWindowsFileInspector()
    repository = LibraryRepository(database, queue)
    scanner = LibraryScanner(database, repository, inspector, queue)
    root = tmp_path / "library"
    root.mkdir()
    (root / "initial.pdf").write_bytes(b"%PDF-1.7\ninitial\n%%EOF")
    library = repository.register(
        inspector.inspect_root_directory(root),
        recursive=True,
        authorization_source="native_picker_confirmed",
    )
    scanner.scan_library(library.id)
    created = root / "later.pdf"
    event_ready = asyncio.Event()

    async def one_event(**_kwargs: object) -> AsyncIterator[set[tuple[int, str]]]:
        await event_ready.wait()
        yield {(1, str(created))}
        await asyncio.Future()

    manager = WatcherManager(repository, scanner, watch=one_event)
    await manager.add_library(library.id)
    created.write_bytes(b"%PDF-1.7\nlater\n%%EOF")
    event_ready.set()

    for _ in range(100):
        with database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if count == 2:
            break
        await asyncio.sleep(0.01)

    assert count == 2
    assert queue.count(kind="parse_pdf") == 2
    await manager.shutdown()
