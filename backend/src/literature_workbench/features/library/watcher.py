from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from watchfiles import awatch

from literature_workbench.features.library.models import LibraryRoot, ScanSummary


class ActiveLibraryRepository(Protocol):
    def get(self, library_id: UUID) -> LibraryRoot: ...

    def list_active(self) -> tuple[LibraryRoot, ...]: ...


class Reconciler(Protocol):
    def reconcile_path(self, library_id: UUID, path: Path) -> ScanSummary: ...

    def reconcile_paths(
        self, library_id: UUID, paths: tuple[Path, ...]
    ) -> ScanSummary: ...

    def scan_library(self, library_id: UUID) -> ScanSummary: ...


WatchBatch = set[tuple[object, str]]
WatchFactory = Callable[..., AsyncIterator[WatchBatch]]


class WatcherManager:
    def __init__(
        self,
        repository: ActiveLibraryRepository,
        scanner: Reconciler,
        *,
        watch: WatchFactory | None = None,
        restart_delay: float = 0.25,
    ) -> None:
        self._repository = repository
        self._scanner = scanner
        self._watch = watch or cast(WatchFactory, awatch)
        self._restart_delay = restart_delay
        self._shutdown = asyncio.Event()
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    @property
    def active_library_ids(self) -> tuple[UUID, ...]:
        return tuple(self._tasks)

    async def add_library(self, library_id: UUID) -> None:
        if self._shutdown.is_set() or library_id in self._tasks:
            return
        library = self._repository.get(library_id)
        if library.status != "active" or library.discovery_mode != "monitored":
            return
        self._tasks[library_id] = asyncio.create_task(
            self._watch_library(library), name=f"library-watch-{library_id}"
        )

    async def remove_library(self, library_id: UUID) -> None:
        task = self._tasks.pop(library_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def reconcile_active_libraries(self) -> None:
        desired = {
            library.id
            for library in self._repository.list_active()
            if library.discovery_mode == "monitored" and library.status == "active"
        }
        for library_id in tuple(self._tasks):
            if library_id not in desired:
                await self.remove_library(library_id)
        for library_id in desired:
            await self.add_library(library_id)

    async def process_changes(
        self,
        library_id: UUID,
        changes: Iterable[tuple[object, str]],
    ) -> None:
        library = self._repository.get(library_id)
        root = Path(os.path.abspath(library.canonical_path))
        candidates: dict[str, Path] = {}
        for _change, raw_path in changes:
            candidate = Path(os.path.abspath(raw_path))
            if not self._contained(candidate, root):
                continue
            candidates[os.path.normcase(str(candidate))] = candidate
        if candidates:
            await asyncio.to_thread(
                self._scanner.reconcile_paths, library_id, tuple(candidates.values())
            )

    async def shutdown(self) -> None:
        self._shutdown.set()
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_library(self, library: LibraryRoot) -> None:
        while not self._shutdown.is_set():
            try:
                async for changes in self._watch(
                    library.canonical_path,
                    recursive=library.recursive,
                    debounce=750,
                    step=1000,
                    stop_event=self._shutdown,
                ):
                    await self.process_changes(library.id, changes)
                    if self._shutdown.is_set():
                        return
                if self._shutdown.is_set():
                    return
                await asyncio.to_thread(self._scanner.scan_library, library.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._shutdown.is_set():
                    return
                try:
                    await asyncio.to_thread(self._scanner.scan_library, library.id)
                except Exception:
                    pass
            if self._restart_delay:
                await asyncio.sleep(self._restart_delay)

    @staticmethod
    def _contained(candidate: Path, root: Path) -> bool:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        root_key = os.path.normcase(os.path.abspath(root))
        try:
            return os.path.commonpath((candidate_key, root_key)) == root_key
        except ValueError:
            return False
