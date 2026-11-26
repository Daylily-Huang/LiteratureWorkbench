"""Bounded ZIP inspection and known-schema validation before a backup is writable."""
from __future__ import annotations

import io
import sqlite3
import struct
import zipfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.backups import BackupError, MAX_ENTRIES, io_path

MAX_METADATA_READ = 64 * 1024**2
CATALOG = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"


class _BoundedReader(io.BufferedReader):
    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            position = self.tell()
            self.seek(0, 2)
            requested = self.tell() - position
            self.seek(position)
        else:
            requested = size
        if requested > MAX_METADATA_READ:
            raise BackupError('backup_too_large')
        return super().read(size)


@contextmanager
def bounded_archive(path: Path) -> Iterator[zipfile.ZipFile]:
    with _BoundedReader(io.FileIO(io_path(path), 'r')) as stream:
        # CPython 3.12 is pinned by this application. Read EOCD/ZIP64 before ZipFile
        # allocates its directory and ZipInfo objects; the bounded reader also caps
        # actual reads when a forged EOCD claims fewer records than its directory.
        end: Any = getattr(zipfile, '_EndRecData')(stream)
        if end is None:
            raise BackupError('backup_manifest_invalid')
        if end[getattr(zipfile, '_ECD_ENTRIES_TOTAL')] > MAX_ENTRIES + 1 or end[getattr(zipfile, '_ECD_SIZE')] > MAX_METADATA_READ:
            raise BackupError('backup_too_large')
        size = end[getattr(zipfile, '_ECD_SIZE')]
        start = end[getattr(zipfile, '_ECD_LOCATION')] - size
        if end[getattr(zipfile, '_ECD_SIGNATURE')] == getattr(zipfile, 'stringEndArchive64'):
            start -= getattr(zipfile, 'sizeEndCentDir64') + getattr(zipfile, 'sizeEndCentDir64Locator')
        if start < 0:
            raise BackupError('backup_manifest_invalid')
        stream.seek(start)
        remaining = size
        count = 0
        while remaining:
            header_size = getattr(zipfile, 'sizeCentralDir')
            header = stream.read(header_size)
            if len(header) != header_size or header[:4] != b'PK\x01\x02':
                raise BackupError('backup_manifest_invalid')
            values = struct.unpack(getattr(zipfile, 'structCentralDir'), header)
            extra = sum(values[getattr(zipfile, key)] for key in ('_CD_FILENAME_LENGTH','_CD_EXTRA_FIELD_LENGTH','_CD_COMMENT_LENGTH'))
            remaining -= header_size + extra
            count += 1
            if count > MAX_ENTRIES + 1:
                raise BackupError('backup_too_large')
            if remaining < 0:
                raise BackupError('backup_manifest_invalid')
            stream.seek(extra, 1)
        if count != end[getattr(zipfile, '_ECD_ENTRIES_TOTAL')]:
            raise BackupError('backup_manifest_invalid')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            yield archive


def check_schema(connection: sqlite3.Connection, directory: Path, declared: object) -> None:
    connection.execute('PRAGMA trusted_schema=OFF')
    reference = Database.open(directory/'expected-schema.sqlite3')
    run_migrations(reference)
    with reference.connect() as expected:
        catalog = [tuple(row) for row in expected.execute(CATALOG)]
        versions = [row[0] for row in expected.execute('SELECT version FROM schema_migrations ORDER BY version')]
    if [tuple(row) for row in connection.execute(CATALOG)] != catalog:
        raise BackupError('backup_schema_unsupported')
    actual = [row[0] for row in connection.execute('SELECT version FROM schema_migrations ORDER BY version')]
    if actual != versions or declared != actual:
        raise BackupError('backup_schema_unsupported')
