from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.platform.windows.filesystem import RootIdentity


class LibraryRepository:
    def __init__(self, database: Database, queue: TaskQueue | None = None) -> None:
        self._database = database
        self._queue = queue

    def count(self) -> int:
        with self._database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM libraries").fetchone()
        if row is None:
            raise RuntimeError("library_count_failed")
        return int(row["count"])

    def get(self, library_id: UUID) -> LibraryRoot:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM libraries WHERE id = ?", (str(library_id),)
            ).fetchone()
        if row is None:
            raise KeyError("library_not_found")
        return self._from_row(row)

    def list_active(self) -> tuple[LibraryRoot, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM libraries WHERE status = 'active' ORDER BY created_at, id"
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def register(
        self,
        identity: RootIdentity,
        *,
        recursive: bool,
        authorization_source: str,
    ) -> LibraryRoot:
        path_key = self._path_key(identity.canonical_path)
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            exact = connection.execute(
                "SELECT * FROM libraries WHERE root_path_key = ?",
                (path_key,),
            ).fetchone()
            if exact is not None:
                existing = self._from_row(exact)
                if (existing.volume_id, existing.file_id) != (
                    identity.volume_id,
                    identity.file_id,
                ):
                    raise ValueError("registered_root_identity_changed")
                if existing.recursive != recursive:
                    raise ValueError("library_recursive_mismatch")
                return existing

            active_roots = connection.execute(
                """
                SELECT * FROM libraries
                WHERE status = 'active' AND discovery_mode = 'monitored'
                """
            ).fetchall()
            if any(
                self._overlaps(identity.canonical_path, Path(row["root_path"]))
                for row in active_roots
            ):
                raise ValueError("overlapping_library_root")

            library_id = str(uuid4())
            try:
                connection.execute(
                    """
                    INSERT INTO libraries (
                      id, root_path, root_path_key, root_volume_id, root_file_id,
                      recursive, discovery_mode, cloud_pdf_auto_allowed,
                      authorization_source, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'monitored', 1, ?, 'active', ?, ?)
                    """,
                    (
                        library_id,
                        str(identity.canonical_path),
                        path_key,
                        identity.volume_id,
                        identity.file_id,
                        int(recursive),
                        authorization_source,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                exact = connection.execute(
                    "SELECT * FROM libraries WHERE root_path_key = ?",
                    (path_key,),
                ).fetchone()
                if exact is None:
                    raise
                return self._from_row(exact)
            if self._queue is not None:
                self._queue.enqueue_in_transaction(
                    connection,
                    "scan_library",
                    f"scan_library:{library_id}",
                    {"library_id": library_id},
                )
                revision_row = connection.execute(
                    "SELECT revision FROM app_settings WHERE key = 'active_library_revision'"
                ).fetchone()
                revision = 1 if revision_row is None else int(revision_row["revision"]) + 1
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value_json, revision, updated_at)
                    VALUES ('active_library_revision', ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                      value_json = excluded.value_json,
                      revision = excluded.revision,
                      updated_at = excluded.updated_at
                    """,
                    (json.dumps(revision), revision, now),
                )
            row = connection.execute(
                "SELECT * FROM libraries WHERE id = ?",
                (library_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("library_insert_failed")
        return self._from_row(row)

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    @classmethod
    def _overlaps(cls, left: Path, right: Path) -> bool:
        left_key = cls._path_key(left)
        right_key = cls._path_key(right)
        try:
            common = os.path.commonpath((left_key, right_key))
        except ValueError:
            return False
        return common in {left_key, right_key}

    @staticmethod
    def _from_row(row: sqlite3.Row) -> LibraryRoot:
        from uuid import UUID

        return LibraryRoot(
            id=UUID(row["id"]),
            canonical_path=Path(row["root_path"]),
            volume_id=str(row["root_volume_id"]),
            file_id=str(row["root_file_id"]),
            recursive=bool(row["recursive"]),
            discovery_mode=str(row["discovery_mode"]),
            authorization_source=str(row["authorization_source"]),
            status=str(row["status"]),
        )
