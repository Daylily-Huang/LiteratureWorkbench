from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from uuid import UUID


@dataclass(slots=True)
class _LibraryLock:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


class LibraryScanCoordinator:
    def __init__(self) -> None:
        self._registry_lock = Lock()
        self._locks: dict[UUID, _LibraryLock] = {}

    @contextmanager
    def acquire(self, library_id: UUID) -> Iterator[None]:
        with self._registry_lock:
            entry = self._locks.setdefault(library_id, _LibraryLock())
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._registry_lock:
                entry.users -= 1
                if entry.users == 0 and self._locks.get(library_id) is entry:
                    del self._locks[library_id]
