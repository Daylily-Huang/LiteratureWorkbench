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
from literature_workbench.features.library.fingerprint import (
    ContentFingerprint,
    fingerprint_verified_pdf,
)
from literature_workbench.features.library.models import (
    ContentId,
    DocumentRecord,
    LibraryRoot,
    ScanSummary,
)
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.filesystem import (
    FileIdentity,
    SafeFileInspector,
    UnsafePath,
    VerifiedHandle,
)


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
        self._require_current_root(library)
        enumerated, unsafe_paths, incomplete, incomplete_paths = self._enumerate(
            library, library.canonical_path, recursive=library.recursive
        )
        final_root_failed = False
        try:
            self._require_current_root(library)
        except (OSError, UnsafePath):
            unsafe_paths += 1
            incomplete = True
            final_root_failed = True
        if final_root_failed:
            enumerated = []
            incomplete_paths = [library.canonical_path]
        return self._scan_candidates(
            library,
            enumerated,
            unsafe_paths=unsafe_paths,
            incomplete=incomplete,
            full_snapshot=True,
            affected_paths=(library.canonical_path,),
            initial_incomplete_paths=tuple(incomplete_paths),
        )

    def reconcile_path(self, library_id: UUID, path: Path) -> ScanSummary:
        return self.reconcile_paths(library_id, (path,))

    def reconcile_paths(self, library_id: UUID, paths: list[Path] | tuple[Path, ...]) -> ScanSummary:
        library = self._repository.get(library_id)
        if library.discovery_mode == "managed_import":
            return ScanSummary(0, 0, 0, 0, ())
        self._require_current_root(library)
        root = Path(os.path.abspath(library.canonical_path))
        normalized: dict[str, Path] = {}
        for raw_path in paths:
            candidate = Path(os.path.abspath(raw_path))
            if not self._contained(candidate, root):
                continue
            normalized[os.path.normcase(str(candidate))] = candidate

        candidates: list[Path] = []
        affected: list[Path] = []
        incomplete_paths: list[Path] = []
        unsafe_paths = 0
        incomplete = False
        for candidate in normalized.values():
            affected.append(candidate)
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                candidates.extend(self._active_peer_paths_for_missing(library, candidate))
                continue
            except OSError:
                unsafe_paths += 1
                incomplete = True
                incomplete_paths.append(candidate)
                continue
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                unsafe_paths += 1
                incomplete = True
                incomplete_paths.append(candidate)
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if candidate == root or library.recursive:
                    found, errors, partial, unsafe_scopes = self._enumerate(
                        library, candidate, recursive=library.recursive
                    )
                    candidates.extend(found)
                    unsafe_paths += errors
                    incomplete = incomplete or partial
                    incomplete_paths.extend(unsafe_scopes)
            elif stat.S_ISREG(metadata.st_mode):
                candidates.append(candidate)

        try:
            self._require_current_root(library)
        except (OSError, UnsafePath):
            unsafe_paths += 1
            incomplete = True

        unique_candidates = {
            os.path.normcase(os.path.abspath(path)): path for path in candidates
        }
        return self._scan_candidates(
            library,
            list(unique_candidates.values()),
            unsafe_paths=unsafe_paths,
            incomplete=incomplete,
            full_snapshot=False,
            affected_paths=tuple(affected),
            initial_incomplete_paths=tuple(incomplete_paths),
        )

    def _scan_candidates(
        self,
        library: LibraryRoot,
        candidates: list[Path],
        *,
        unsafe_paths: int,
        incomplete: bool,
        full_snapshot: bool,
        affected_paths: tuple[Path, ...],
        initial_incomplete_paths: tuple[Path, ...] = (),
    ) -> ScanSummary:
        discovered: list[_DiscoveredFile] = []
        incomplete_paths = list(initial_incomplete_paths)
        for path in candidates:
            try:
                item = self._discover_file(path, library)
            except (OSError, UnsafePath, ValueError):
                unsafe_paths += 1
                incomplete = True
                incomplete_paths.append(path)
                continue
            discovered.append(item)

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
            incomplete=incomplete,
            full_snapshot=full_snapshot,
            affected_paths=affected_paths,
            incomplete_paths=tuple(incomplete_paths),
        )
        return ScanSummary(
            pdf_paths=len(pdf_hashes),
            unique_contents=len(counts),
            duplicate_groups=sum(1 for count in counts.values() if count > 1),
            unsupported_paths=unsupported_paths,
            queued_content_ids=tuple(ContentId(value) for value in queued),
            unsafe_paths=unsafe_paths,
        )

    def _discover_file(self, path: Path, library: LibraryRoot) -> _DiscoveredFile:
        if path.suffix.casefold() == ".pdf":
            seed = self._inspector.inspect_under_root(path, library)
            first_identity, first_fingerprint = self._stable_fingerprint(
                self._open_pdf(library, seed)
            )
            second_identity, second_fingerprint = self._stable_fingerprint(
                self._open_pdf(library, first_identity)
            )
            if (
                self._identity_key(first_identity) != self._identity_key(second_identity)
                or first_fingerprint != second_fingerprint
            ):
                raise UnsafePath("pdf_changed_during_hash")
            final_identity = self._inspector.inspect_under_root(path, library)
            if self._identity_key(second_identity) != self._identity_key(final_identity):
                raise UnsafePath("pdf_changed_after_hash")
            identity = second_identity
            fingerprint = second_fingerprint
            status = "candidate_pdf"
            authorization_source = "registered_library"
            file_format = "pdf"
        else:
            identity = self._inspector.inspect_regular_file(path, library)
            fingerprint = None
            status = "unsupported_v01"
            authorization_source = "unsupported_format"
            file_format = identity.canonical_path.suffix.casefold().lstrip(".") or "unknown"

        relative_path = Path(
            os.path.relpath(identity.canonical_path, library.canonical_path)
        ).as_posix()
        return _DiscoveredFile(
            path=identity.canonical_path,
            relative_path=relative_path,
            display_name=identity.canonical_path.name,
            format=file_format,
            size_bytes=identity.size,
            modified_ns=identity.modified_ns,
            volume_id=identity.volume_id,
            file_id=identity.file_id,
            hard_link_count=identity.hard_link_count,
            content_sha256=None if fingerprint is None else fingerprint.sha256,
            status=status,
            authorization_source=authorization_source,
        )

    def _open_pdf(self, library: LibraryRoot, identity: FileIdentity) -> VerifiedHandle:
        document = DocumentRecord(
            id=uuid4(),
            library_root=library,
            path=identity.canonical_path,
            volume_id=identity.volume_id,
            file_id=identity.file_id,
            size_bytes=identity.size,
            modified_ns=identity.modified_ns,
        )
        return self._inspector.open_registered_pdf(document)

    @staticmethod
    def _stable_fingerprint(
        handle: VerifiedHandle,
    ) -> tuple[FileIdentity, ContentFingerprint]:
        try:
            before = handle.current_identity()
            fingerprint = fingerprint_verified_pdf(handle)
            after = handle.current_identity()
        finally:
            handle.close()
        if LibraryScanner._identity_key(before) != LibraryScanner._identity_key(after):
            raise UnsafePath("pdf_changed_during_hash")
        if fingerprint.size_bytes != after.size:
            raise UnsafePath("pdf_size_changed_during_hash")
        return after, fingerprint

    def _require_current_root(self, library: LibraryRoot) -> None:
        current_root = self._inspector.inspect_root_directory(library.canonical_path)
        if (current_root.volume_id, current_root.file_id) != (
            library.volume_id,
            library.file_id,
        ):
            raise UnsafePath("root_identity_changed")

    def _active_peer_paths_for_missing(
        self, library: LibraryRoot, missing_path: Path
    ) -> list[Path]:
        path_key = os.path.normcase(os.path.abspath(missing_path))
        with self._database.connect() as connection:
            missing = connection.execute(
                """
                SELECT volume_id, file_id FROM documents
                WHERE library_id = ? AND path_key = ?
                """,
                (str(library.id), path_key),
            ).fetchone()
            if missing is None:
                return []
            peers = connection.execute(
                """
                SELECT path FROM documents
                WHERE library_id = ? AND volume_id = ? AND file_id = ?
                  AND path_key <> ? AND status <> 'missing'
                """,
                (
                    str(library.id),
                    str(missing["volume_id"]),
                    str(missing["file_id"]),
                    path_key,
                ),
            ).fetchall()
        return [Path(str(row["path"])) for row in peers]

    def _enumerate(
        self,
        library: LibraryRoot,
        start: Path,
        *,
        recursive: bool,
    ) -> tuple[list[Path], int, bool, list[Path]]:
        files: list[Path] = []
        incomplete_paths: list[Path] = []
        unsafe_paths = 0
        incomplete = False
        pending = [start]
        while pending:
            directory = pending.pop()
            try:
                with self._inspector.hold_directory_for_enumeration(directory, library):
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            try:
                                metadata = entry.stat(follow_symlinks=False)
                            except OSError:
                                unsafe_paths += 1
                                incomplete = True
                                entry_path = getattr(entry, "path", None)
                                if entry_path is not None:
                                    incomplete_paths.append(Path(entry_path))
                                continue
                            attributes = int(getattr(metadata, "st_file_attributes", 0))
                            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                                unsafe_paths += 1
                                incomplete = True
                                incomplete_paths.append(Path(entry.path))
                                continue
                            path = Path(entry.path)
                            if stat.S_ISDIR(metadata.st_mode):
                                if recursive:
                                    pending.append(path)
                            elif stat.S_ISREG(metadata.st_mode):
                                files.append(path)
            except (OSError, UnsafePath):
                unsafe_paths += 1
                incomplete = True
                incomplete_paths.append(directory)
        files.sort(key=lambda path: path.as_posix().casefold())
        return files, unsafe_paths, incomplete, incomplete_paths

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
        incomplete: bool,
        full_snapshot: bool,
        affected_paths: tuple[Path, ...],
        incomplete_paths: tuple[Path, ...],
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
                        if self._eligible_rename_source(
                            candidate,
                            discovered_keys=discovered_keys,
                            full_snapshot=full_snapshot,
                            affected_paths=affected_paths,
                        )
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
                    preserve_observation = not version_changed and str(row["status"]) in {
                        "candidate_pdf",
                        "valid_pdf",
                        "unsupported_v01",
                    }
                    target_status = (
                        str(row["status"]) if preserve_observation else item.status
                    )
                    target_auto = (
                        int(row["auto_cloud_eligible"])
                        if preserve_observation
                        and item.content_sha256 is not None
                        and item.hard_link_count == 1
                        else 0
                    )
                    connection.execute(
                        """
                        UPDATE documents SET
                          content_sha256 = ?, path = ?, path_key = ?, display_name = ?,
                          format = ?, size_bytes = ?, mtime_ns = ?, volume_id = ?, file_id = ?,
                          hard_link_count = ?, auto_cloud_eligible = ?,
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
                            target_auto,
                            item.authorization_source,
                            target_status,
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
                if item.content_sha256 is not None and item.hard_link_count > 1:
                    for peer in by_identity.get(item.identity, []):
                        if (
                            str(peer["id"]) == document_id
                            or str(peer["path_key"]) in discovered_keys
                            or str(peer["status"]) == "missing"
                        ):
                            continue
                        peer_changed = any(
                            (
                                peer["content_sha256"] != item.content_sha256,
                                int(peer["size_bytes"]) != item.size_bytes,
                                int(peer["mtime_ns"]) != item.modified_ns,
                                int(peer["hard_link_count"]) != item.hard_link_count,
                            )
                        )
                        connection.execute(
                            """
                            UPDATE documents SET
                              content_sha256 = ?, size_bytes = ?, mtime_ns = ?,
                              hard_link_count = ?, status = 'candidate_pdf',
                              auto_cloud_eligible = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                item.content_sha256,
                                item.size_bytes,
                                item.modified_ns,
                                item.hard_link_count,
                                now,
                                str(peer["id"]),
                            ),
                        )
                        if peer_changed:
                            connection.execute(
                                """
                                INSERT INTO document_versions (
                                  id, document_id, content_sha256, size_bytes, mtime_ns,
                                  volume_id, file_id, hard_link_count, observed_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(uuid4()),
                                    str(peer["id"]),
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
                if any(
                    self._contained(Path(str(row["path"])), path)
                    for path in incomplete_paths
                ):
                    connection.execute(
                        """
                        UPDATE documents
                        SET status = 'incomplete', auto_cloud_eligible = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, document_id),
                    )
                elif document_id not in seen_ids and self._should_mark_missing(
                    Path(str(row["path"])),
                    full_snapshot=full_snapshot,
                    snapshot_complete=not incomplete,
                    affected_paths=affected_paths,
                ):
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
            inventory = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN format = 'pdf' THEN 1 ELSE 0 END) AS pdf_paths,
                  COUNT(DISTINCT CASE WHEN format = 'pdf' THEN content_sha256 END)
                    AS unique_contents,
                  SUM(CASE WHEN format <> 'pdf' THEN 1 ELSE 0 END) AS unsupported_paths
                FROM documents
                WHERE library_id = ? AND status NOT IN ('missing', 'incomplete')
                """,
                (str(library.id),),
            ).fetchone()
            duplicate_row = connection.execute(
                """
                SELECT COUNT(*) AS duplicate_groups FROM (
                  SELECT content_sha256 FROM documents
                  WHERE library_id = ? AND format = 'pdf'
                    AND status NOT IN ('missing', 'incomplete')
                    AND content_sha256 IS NOT NULL
                  GROUP BY content_sha256 HAVING COUNT(*) > 1
                )
                """,
                (str(library.id),),
            ).fetchone()
            warnings: list[str] = []
            if unsafe_paths:
                warnings.append("unsafe_paths_skipped")
            if incomplete:
                warnings.append("incomplete_scan")
            status_value = {
                "pdf_paths": int(inventory["pdf_paths"] or 0),
                "unique_contents": int(inventory["unique_contents"] or 0),
                "duplicate_groups": int(duplicate_row["duplicate_groups"] or 0),
                "unsupported_paths": int(inventory["unsupported_paths"] or 0),
                "unsafe_paths": unsafe_paths,
                "warnings": warnings,
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

    @staticmethod
    def _identity_key(identity: FileIdentity) -> tuple[str, str, str, int, int, int]:
        return (
            os.path.normcase(os.path.abspath(identity.canonical_path)),
            identity.volume_id,
            identity.file_id,
            identity.size,
            identity.modified_ns,
            identity.hard_link_count,
        )

    @staticmethod
    def _contained(candidate: Path, root: Path) -> bool:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        root_key = os.path.normcase(os.path.abspath(root))
        try:
            return os.path.commonpath((candidate_key, root_key)) == root_key
        except ValueError:
            return False

    @classmethod
    def _should_mark_missing(
        cls,
        document_path: Path,
        *,
        full_snapshot: bool,
        snapshot_complete: bool,
        affected_paths: tuple[Path, ...],
    ) -> bool:
        if full_snapshot:
            return snapshot_complete
        return any(cls._contained(document_path, path) for path in affected_paths)

    @classmethod
    def _eligible_rename_source(
        cls,
        row: sqlite3.Row,
        *,
        discovered_keys: set[str],
        full_snapshot: bool,
        affected_paths: tuple[Path, ...],
    ) -> bool:
        if str(row["path_key"]) in discovered_keys:
            return False
        if full_snapshot:
            return True
        row_path = Path(str(row["path"]))
        return any(cls._contained(row_path, path) for path in affected_paths)
