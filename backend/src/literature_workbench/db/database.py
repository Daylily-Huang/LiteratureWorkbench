from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Database:
    path: Path

    @classmethod
    def open(cls, path: Path) -> Database:
        resolved_path = Path(path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(resolved_path, check_same_thread=True)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        return cls(resolved_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            check_same_thread=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


def canonical_path_values(path: Path) -> tuple[str, str]:
    canonical = str(path.resolve(strict=True))
    return canonical, os.path.normcase(canonical)
