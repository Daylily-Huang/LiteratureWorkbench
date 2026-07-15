from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.fingerprint import fingerprint_verified_pdf
from literature_workbench.features.library.models import (
    ContentId,
    DocumentRecord,
    LibraryRoot,
    ScanSummary,
)
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.filesystem import SafeFileInspector, UnsafePath


@dataclass(frozen=True, slots=True)
class _DiscoveredFile:
    path: Path
    relative_path: str
    display_name: str
    format: str
    size_bytes: int
    modified_ns: int
    volume_id: str
    file_id: str
    hard_link_count: int
    content_sha256: str | None
    status: str
    authorization_source: str

    @property
    def path_key(self) -> str:
        return os.path.normcase(os.path.abspath(self.path))

    @property
    def identity(self) -> tuple[str, str]:
        return self.volume_id, self.file_id


class LibraryScanner:
    def __init__(
        self,
        database: Database,
        repository: LibraryRepository,
        inspector: SafeFileInspector,
        queue: TaskQueue,
    ) -> None:
        self._database = database
        self._repository = repository
        self._inspector = inspector
        self._queue = queue

    def scan_library(self, library_id: UUID) -> ScanSummary:
        library = self._repository.get(library_id)
        if library.discovery_mode == "managed_import":
            return ScanSummary(0, 0, 0, 0, ())
        current_root = self._inspector.inspect_root_directory(library.canonical_path)
        if (current_root.volume_id, current_root.file_id) != (
            library.volume_id,
            library.file_id,
        ):
            raise UnsafePath("root_identity_changed")

        discovered: list[_DiscoveredFile] = []
        enumerated, unsafe_paths = self._enumerate(library)
        for path, entry_stat in enumerated:
            relative_path = path.relative_to(library.canonical_path).as_posix()
            if path.suffix.casefold() == ".pdf":
                try:
                    identity = self._inspector.inspect_under_root(path, library)
                    document = DocumentRecord(
                        id=uuid4(),
                        library_root=library,
                        path=identity.canonical_path,
                        volume_id=identity.volume_id,
                        file_id=identity.file_id,
                        size_bytes=identity.size,
                        modified_ns=identity.modified_ns,
                    )
                    handle = self._inspector.open_registered_pdf(document)
                    try:
                        fingerprint = fingerprint_verified_pdf(handle)
                    finally:
                        handle.close()
                except (OSError, UnsafePath, ValueError):
                    unsafe_paths += 1
                    continue
                discovered.append(
                    _DiscoveredFile(
                        path=identity.canonical_path,
                        relative_path=relative_path,
                        display_name=path.name,
                        format="pdf",
                        size_bytes=fingerprint.size_bytes,
                        modified_ns=identity.modified_ns,
                        volume_id=identity.volume_id,
                        file_id=identity.file_id,
                        hard_link_count=identity.hard_link_count,
                        content_sha256=fingerprint.sha256,
                        status="candidate_pdf",
                        authorization_source="registered_library",
                    )
                )
            else:
                discovered.append(
                    _DiscoveredFile(
                        path=path,
                        relative_path=relative_path,
                        display_name=path.name,
                        format=path.suffix.casefold().lstrip(".") or "unknown",
                        size_bytes=entry_stat.st_size,
                        modified_ns=entry_stat.st_mtime_ns,
                        volume_id=f"{entry_stat.st_dev:016x}",
                        file_id=f"{entry_stat.st_ino:032x}",
                        hard_link_count=entry_stat.st_nlink,
                        content_sha256=None,
                        status="unsupported_v01",
                        authorization_source="unsupported_format",
                    )
                )

        pdf_hashes = [item.content_sha256 for item in discovered if item.content_sha256]
        counts = Counter(pdf_hashes)
        unsupported_paths = sum(1 for item in discovered if item.content_sha256 is None)
        queued = self._reconcile(
            library,
            discovered,
            pdf_paths=len(pdf_hashes),
            unique_contents=len(counts),
            duplicate_groups=sum(1 for count in counts.values() if count > 1),
            unsupported_paths=unsupported_paths,
            unsafe_paths=unsafe_paths,
        )
        return ScanSummary(
            pdf_paths=len(pdf_hashes),
            unique_contents=len(counts),
            duplicate_groups=sum(1 for count in counts.values() if count > 1),
            unsupported_paths=unsupported_paths,
            queued_content_ids=tuple(ContentId(value) for value in queued),
            unsafe_paths=unsafe_paths,
        )

    def reconcile_path(self, library_id: UUID, _path: Path) -> ScanSummary:
        return self.scan_library(library_id)

    def _enumerate(
        self, library: LibraryRoot
    ) -> tuple[list[tuple[Path, os.stat_result]], int]:
        files: list[tuple[Path, os.stat_result]] = []
        unsafe_paths = 0
        pending = [library.canonical_path]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attributes = int(getattr(metadata, "st_file_attributes", 0))
                    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        unsafe_paths += 1
                        continue
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if library.recursive:
                            pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        files.append((path, metadata))
        files.sort(key=lambda item: item[0].as_posix().casefold())
        return files, unsafe_paths

    def _reconcile(
        self,
        library: LibraryRoot,
        discovered: list[_DiscoveredFile],
        *,
        pdf_paths: int,
        unique_contents: int,
        duplicate_groups: int,
        unsupported_paths: int,
        unsafe_paths: int,
    ) -> list[str]:
        now = datetime.now(UTC).isoformat()
        discovered_keys = {item.path_key for item in discovered}
        identity_counts = Counter(item.identity for item in discovered)
        queued: list[str] = []
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM documents WHERE library_id = ?", (str(library.id),)
            ).fetchall()
            by_path = {str(row["path_key"]): row for row in existing}
            by_identity: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for row in existing:
                by_identity.setdefault(
                    (str(row["volume_id"]), str(row["file_id"])), []
                ).append(row)

            seen_ids: set[str] = set()
            for item in discovered:
                row = by_path.get(item.path_key)
                if row is None and identity_counts[item.identity] == 1:
                    absent_matches = [
                        candidate
                        for candidate in by_identity.get(item.identity, [])
                        if str(candidate["path_key"]) not in discovered_keys
                        and str(candidate["id"]) not in seen_ids
                    ]
                    if len(absent_matches) == 1:
                        row = absent_matches[0]
                document_id = str(uuid4()) if row is None else str(row["id"])
                seen_ids.add(document_id)

                if item.content_sha256 is not None:
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO contents (
                          sha256, size_bytes, mime_type, page_count, current_parse_run_id,
                          title, authors_json, status, created_at, updated_at
                        ) VALUES (?, ?, 'application/pdf', NULL, NULL, NULL, NULL,
                                  'candidate_pdf', ?, ?)
                        """,
                        (item.content_sha256, item.size_bytes, now, now),
                    )
                    if inserted.rowcount == 1:
                        self._queue.enqueue_in_transaction(
                            connection,
                            "parse_pdf",
                            f"parse_pdf:{item.content_sha256}",
                            {"content_id": item.content_sha256},
                        )
                        queued.append(item.content_sha256)

                if row is None:
                    connection.execute(
                        """
                        INSERT INTO documents (
                          id, library_id, content_sha256, path, path_key, display_name,
                          format, size_bytes, mtime_ns, volume_id, file_id, hard_link_count,
                          parse_preference, parse_strategy_override, ocr_language_override,
                          auto_cloud_eligible, authorization_source, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parse', NULL, NULL,
                                  0, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            str(library.id),
                            item.content_sha256,
                            str(item.path),
                            item.path_key,
                            item.display_name,
                            item.format,
                            item.size_bytes,
                            item.modified_ns,
                            item.volume_id,
                            item.file_id,
                            item.hard_link_count,
                            item.authorization_source,
                            item.status,
                            now,
                            now,
                        ),
                    )
                    version_changed = True
                else:
                    version_changed = any(
                        (
                            row["content_sha256"] != item.content_sha256,
                            int(row["size_bytes"]) != item.size_bytes,
                            int(row["mtime_ns"]) != item.modified_ns,
                            str(row["volume_id"]) != item.volume_id,
                            str(row["file_id"]) != item.file_id,
                            int(row["hard_link_count"]) != item.hard_link_count,
                        )
                    )
                    connection.execute(
                        """
                        UPDATE documents SET
                          content_sha256 = ?, path = ?, path_key = ?, display_name = ?,
                          format = ?, size_bytes = ?, mtime_ns = ?, volume_id = ?, file_id = ?,
                          hard_link_count = ?, auto_cloud_eligible = 0,
                          authorization_source = ?, status = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            item.content_sha256,
                            str(item.path),
                            item.path_key,
                            item.display_name,
                            item.format,
                            item.size_bytes,
                            item.modified_ns,
                            item.volume_id,
                            item.file_id,
                            item.hard_link_count,
                            item.authorization_source,
                            item.status,
                            now,
                            document_id,
                        ),
                    )
                if version_changed:
                    connection.execute(
                        """
                        INSERT INTO document_versions (
                          id, document_id, content_sha256, size_bytes, mtime_ns,
                          volume_id, file_id, hard_link_count, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            document_id,
                            item.content_sha256,
                            item.size_bytes,
                            item.modified_ns,
                            item.volume_id,
                            item.file_id,
                            item.hard_link_count,
                            now,
                        ),
                    )

            for row in existing:
                document_id = str(row["id"])
                if document_id not in seen_ids:
                    connection.execute(
                        """
                        UPDATE documents
                        SET status = 'missing', auto_cloud_eligible = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, document_id),
                    )
            status_key = f"library_scan_status:{library.id}"
            status_row = connection.execute(
                "SELECT revision FROM app_settings WHERE key = ?", (status_key,)
            ).fetchone()
            status_revision = 1 if status_row is None else int(status_row["revision"]) + 1
            status_value = {
                "pdf_paths": pdf_paths,
                "unique_contents": unique_contents,
                "duplicate_groups": duplicate_groups,
                "unsupported_paths": unsupported_paths,
                "unsafe_paths": unsafe_paths,
                "warnings": ["unsafe_paths_skipped"] if unsafe_paths else [],
            }
            connection.execute(
                """
                INSERT INTO app_settings (key, value_json, revision, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  revision = excluded.revision,
                  updated_at = excluded.updated_at
                """,
                (
                    status_key,
                    json.dumps(status_value, separators=(",", ":")),
                    status_revision,
                    now,
                ),
            )
        return queued
