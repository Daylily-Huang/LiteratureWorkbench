from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.filesystem import SafeFileInspector, UnsafePath


LOGGER = logging.getLogger("literature_workbench.annotations")
APP_TITLE = "Literature Workbench"
APP_SUBJECT_PREFIX = "lw:"
ANNOTATION_COLORS: dict[str, tuple[float, float, float]] = {
    "yellow": (1.0, 0.84, 0.0),
    "green": (0.35, 0.78, 0.42),
    "blue": (0.35, 0.68, 0.95),
    "pink": (0.96, 0.47, 0.69),
    "red": (0.9, 0.16, 0.16),
    "black": (0.08, 0.08, 0.08),
}


class AnnotationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DocumentState:
    document_id: UUID
    library_id: UUID
    path: Path
    filename: str
    document_version_id: UUID
    content_id: str
    size_bytes: int
    mtime_ns: int
    volume_id: str
    file_id: str
    hard_link_count: int


class _DocumentLocks:
    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[UUID, tuple[Lock, int]] = {}

    @contextmanager
    def acquire(self, document_id: UUID) -> Iterator[None]:
        with self._guard:
            lock, users = self._locks.get(document_id, (Lock(), 0))
            self._locks[document_id] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current, users = self._locks[document_id]
                if users == 1:
                    del self._locks[document_id]
                else:
                    self._locks[document_id] = (current, users - 1)


class PdfAnnotationService:
    def __init__(
        self,
        database: Database,
        data_root: Path,
        queue: TaskQueue,
        library_repository: LibraryRepository,
        file_inspector: SafeFileInspector,
    ) -> None:
        self._database = database
        self._backup_root = data_root / ".annotation-backups"
        self._queue = queue
        self._library_repository = library_repository
        self._file_inspector = file_inspector
        self._locks = _DocumentLocks()
        self.reconcile_crashed_mutations()

    def list_annotations(self, document_id: UUID) -> dict[str, object]:
        state = self._load_state(document_id)
        self._verify_current_file(state)
        with fitz.open(state.path) as document:
            items = [
                self._serialize_annotation(page, annotation)
                for page in document
                for annotation in (page.annots() or ())
            ]
        return {"items": items, **self._current_identity(state.document_id)}

    def create(
        self,
        document_id: UUID,
        *,
        document_version_id: UUID,
        content_id: str,
        page_number: int,
        annotation_type: str,
        quads: list[list[float]],
        color: str,
        comment: str,
    ) -> dict[str, object]:
        if annotation_type not in {"highlight", "underline", "comment"}:
            raise AnnotationError("annotation_type_invalid")
        if color not in ANNOTATION_COLORS:
            raise AnnotationError("annotation_color_invalid")
        if not quads or len(quads) > 200 or any(len(quad) != 8 for quad in quads):
            raise AnnotationError("annotation_quads_invalid")

        def mutate(document: fitz.Document) -> str:
            if page_number < 1 or page_number > document.page_count:
                raise AnnotationError("annotation_page_invalid")
            page = document[page_number - 1]
            converted = [self._pdf_quad_to_page(page, quad) for quad in quads]
            if annotation_type == "underline":
                annotation = page.add_underline_annot(converted)
            else:
                annotation = page.add_highlight_annot(converted)
            effective_color = "blue" if annotation_type == "comment" else color
            annotation.set_colors(stroke=ANNOTATION_COLORS[effective_color])
            annotation.set_info(
                title=APP_TITLE,
                subject=f"{APP_SUBJECT_PREFIX}{annotation_type}:v1",
                content=comment if annotation_type == "comment" else "",
            )
            annotation.update(opacity=0.32 if annotation_type != "underline" else 1.0)
            return self._annotation_id(annotation)

        return self._mutate(
            document_id,
            document_version_id=document_version_id,
            content_id=content_id,
            operation="create",
            mutation=mutate,
        )

    def update(
        self,
        document_id: UUID,
        annotation_id: str,
        *,
        document_version_id: UUID,
        content_id: str,
        color: str | None,
        comment: str | None,
    ) -> dict[str, object]:
        if color is not None and color not in ANNOTATION_COLORS:
            raise AnnotationError("annotation_color_invalid")

        def mutate(document: fitz.Document) -> str:
            _, annotation = self._find_annotation(document, annotation_id)
            self._require_owned(annotation)
            annotation_type = self._owned_type(annotation)
            if color is not None:
                annotation.set_colors(stroke=ANNOTATION_COLORS[color])
            if comment is not None:
                if annotation_type != "comment":
                    raise AnnotationError("annotation_comment_not_editable")
                annotation.set_info(content=comment)
            annotation.update(opacity=0.32 if annotation_type != "underline" else 1.0)
            return annotation_id

        return self._mutate(
            document_id,
            document_version_id=document_version_id,
            content_id=content_id,
            operation="update",
            mutation=mutate,
        )

    def delete(
        self,
        document_id: UUID,
        annotation_id: str,
        *,
        document_version_id: UUID,
        content_id: str,
    ) -> dict[str, object]:
        def mutate(document: fitz.Document) -> str:
            page, annotation = self._find_annotation(document, annotation_id)
            self._require_owned(annotation)
            page.delete_annot(annotation)
            return annotation_id

        return self._mutate(
            document_id,
            document_version_id=document_version_id,
            content_id=content_id,
            operation="delete",
            mutation=mutate,
        )

    def undo(self, document_id: UUID) -> dict[str, object]:
        with self._locks.acquire(document_id):
            state = self._load_state(document_id)
            current_hash = self._verify_current_file(state)
            with self._database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM pdf_annotation_mutations
                    WHERE document_id = ? AND undone_at IS NULL
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (str(document_id),),
                ).fetchone()
            if row is None:
                raise AnnotationError("annotation_undo_unavailable")
            if current_hash != str(row["post_content_sha256"]):
                raise AnnotationError("annotation_external_change")
            history_target = Path(str(row["backup_path"]))
            if not history_target.is_file():
                raise AnnotationError("annotation_backup_missing")

            safety_backup = self._create_snapshot(state, current_hash)
            journal = self._write_journal(state, current_hash, safety_backup)
            committed = False
            try:
                shutil.copy2(history_target, state.path)
                restored_hash = self._hash_file(state.path)
                if restored_hash != str(row["pre_content_sha256"]):
                    raise AnnotationError("annotation_restore_failed")
                identity = self._record_undo_version(state, str(row["id"]))
                committed = True
                self._clear_journal(journal)
            except BaseException:
                if not committed:
                    if self._hash_file(state.path) != current_hash and safety_backup.is_file():
                        shutil.copy2(safety_backup, state.path)
                    self._clear_journal(journal)
                raise
            self._safe_prune_snapshots(state.document_id)
            return identity

    def restore_baseline(self, document_id: UUID, *, confirm_filename: str) -> dict[str, object]:
        with self._locks.acquire(document_id):
            state = self._load_state(document_id)
            if confirm_filename != state.filename:
                raise AnnotationError("annotation_restore_confirmation_mismatch")
            self._verify_current_file(state)
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM pdf_annotation_baselines WHERE document_id = ?",
                    (str(document_id),),
                ).fetchone()
            if row is None:
                raise AnnotationError("annotation_baseline_missing")
            baseline = Path(str(row["backup_path"]))
            if not baseline.is_file():
                raise AnnotationError("annotation_backup_missing")
            pre_hash = self._hash_file(state.path)
            backup = self._create_snapshot(state, pre_hash)
            journal = self._write_journal(state, pre_hash, backup)
            committed = False
            try:
                shutil.copy2(baseline, state.path)
                post_hash = self._hash_file(state.path)
                if post_hash != str(row["content_sha256"]):
                    raise AnnotationError("annotation_restore_failed")
                identity = self._record_version_and_mutation(
                    state,
                    operation="restore",
                    annotation_id=None,
                    pre_hash=pre_hash,
                    post_hash=post_hash,
                    backup=backup,
                )
                committed = True
                self._clear_journal(journal)
            except BaseException:
                if not committed:
                    if self._hash_file(state.path) != pre_hash:
                        shutil.copy2(backup, state.path)
                    self._clear_journal(journal)
                raise
            self._safe_prune_snapshots(state.document_id)
            return identity

    def _mutate(
        self,
        document_id: UUID,
        *,
        document_version_id: UUID,
        content_id: str,
        operation: str,
        mutation: Callable[[fitz.Document], str],
    ) -> dict[str, object]:
        with self._locks.acquire(document_id):
            state = self._load_state(document_id)
            if state.document_version_id != document_version_id or state.content_id != content_id:
                raise AnnotationError("annotation_document_version_conflict")
            pre_hash = self._verify_current_file(state)
            self._ensure_baseline(state, pre_hash)
            backup = self._create_snapshot(state, pre_hash)
            journal = self._write_journal(state, pre_hash, backup)
            committed = False
            try:
                with fitz.open(state.path) as document:
                    if document.is_encrypted or not document.can_save_incrementally():
                        raise AnnotationError("annotation_pdf_not_incremental")
                    annotation_id = mutation(document)
                    document.saveIncr()
                with fitz.open(state.path) as verification:
                    if operation != "delete":
                        self._find_annotation(verification, annotation_id)
                post_hash = self._hash_file(state.path)
                if post_hash == pre_hash:
                    raise AnnotationError("annotation_write_not_persisted")
                identity = self._record_version_and_mutation(
                    state,
                    operation=operation,
                    annotation_id=annotation_id,
                    pre_hash=pre_hash,
                    post_hash=post_hash,
                    backup=backup,
                )
                committed = True
                self._clear_journal(journal)
            except BaseException:
                if not committed:
                    if self._hash_file(state.path) != pre_hash:
                        shutil.copy2(backup, state.path)
                    self._clear_journal(journal)
                raise
            self._safe_prune_snapshots(state.document_id)
            return {"annotation_id": annotation_id, **identity}

    def _write_journal(self, state: DocumentState, pre_hash: str, backup: Path) -> Path:
        self._backup_root.mkdir(parents=True, exist_ok=True)
        journal_path = self._backup_root / f"journal-{state.document_id}.json"
        temp_path = self._backup_root / f"journal-{state.document_id}.tmp"
        record = {
            "document_id": str(state.document_id),
            "library_id": str(state.library_id),
            "path": str(state.path),
            "pre_hash": pre_hash,
            "backup_path": str(backup),
            "volume_id": state.volume_id,
            "file_id": state.file_id,
            "hard_link_count": state.hard_link_count,
            "created_at": datetime.now(UTC).isoformat(),
        }
        temp_path.write_text(json.dumps(record), encoding="utf-8")
        temp_path.replace(journal_path)
        return journal_path

    def _clear_journal(self, journal_path: Path) -> None:
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            pass

    def reconcile_crashed_mutations(self) -> None:
        if not self._backup_root.is_dir():
            return
        for journal_file in self._backup_root.glob("journal-*.json"):
            try:
                record = json.loads(journal_file.read_text(encoding="utf-8"))
                doc_id = str(record["document_id"])
                target_path = Path(str(record["path"]))
                pre_hash = str(record["pre_hash"])
                backup_path = Path(str(record["backup_path"]))

                with self._database.connect() as connection:
                    row = connection.execute(
                        """
                        SELECT library_id, content_sha256, status, volume_id, file_id, hard_link_count
                        FROM documents WHERE id = ?
                        """,
                        (doc_id,),
                    ).fetchone()
                if row is None:
                    journal_file.unlink(missing_ok=True)
                    continue
                if row["content_sha256"] != pre_hash:
                    # DB was already committed prior to crash; no recovery needed.
                    journal_file.unlink(missing_ok=True)
                    continue

                # Document DB record is still at pre_hash. Check if recovery is required and safe.
                if not target_path.exists():
                    LOGGER.warning(
                        "Crash recovery conflict for document %s: target file %s does not exist. "
                        "Preserving backup and journal.",
                        doc_id,
                        target_path,
                    )
                    continue

                if not backup_path.is_file() or self._hash_file(backup_path) != pre_hash:
                    LOGGER.warning(
                        "Crash recovery conflict for document %s: backup file %s is missing or corrupted. "
                        "Preserving target file, backup, and journal.",
                        doc_id,
                        backup_path,
                    )
                    continue

                raw_library_id = record.get("library_id") or row["library_id"]
                try:
                    library_id = UUID(str(raw_library_id))
                    library = self._library_repository.get(library_id)
                except (KeyError, ValueError):
                    LOGGER.warning(
                        "Crash recovery conflict for document %s: library %s not found. "
                        "Preserving target file, backup, and journal.",
                        doc_id,
                        raw_library_id,
                    )
                    continue

                if library.status != "active":
                    LOGGER.warning(
                        "Crash recovery conflict for document %s: library %s is %s. "
                        "Preserving target file, backup, and journal.",
                        doc_id,
                        library_id,
                        library.status,
                    )
                    continue

                try:
                    identity = self._file_inspector.inspect_under_root(target_path, library)
                except (KeyError, OSError, UnsafePath) as inspect_err:
                    LOGGER.warning(
                        "Crash recovery conflict for document %s: target path %s unsafe or invalid: %s. "
                        "Preserving target file, backup, and journal.",
                        doc_id,
                        target_path,
                        inspect_err,
                    )
                    continue

                expected_volume_id = str(record.get("volume_id") or row["volume_id"])
                expected_file_id = str(record.get("file_id") or row["file_id"])
                expected_hard_links = int(record.get("hard_link_count") or row["hard_link_count"])

                if (
                    identity.volume_id != expected_volume_id
                    or identity.file_id != expected_file_id
                    or identity.hard_link_count != expected_hard_links
                    or identity.hard_link_count != 1
                ):
                    LOGGER.warning(
                        "Crash recovery conflict for document %s: file identity mismatch on %s "
                        "(expected volume=%s file=%s links=%s; observed volume=%s file=%s links=%s). "
                        "Preserving target file, backup, and journal.",
                        doc_id,
                        target_path,
                        expected_volume_id,
                        expected_file_id,
                        expected_hard_links,
                        identity.volume_id,
                        identity.file_id,
                        identity.hard_link_count,
                    )
                    continue

                current_hash = self._hash_file(target_path)
                if current_hash == pre_hash:
                    # File on disk is already clean.
                    journal_file.unlink(missing_ok=True)
                else:
                    # Normal half-written mutation: safely restore from verified backup.
                    try:
                        shutil.copy2(backup_path, target_path)
                        restored_hash = self._hash_file(target_path)
                        if restored_hash != pre_hash:
                            LOGGER.error(
                                "Crash recovery failed for document %s: restored hash mismatch. "
                                "Preserving journal.",
                                doc_id,
                            )
                            continue
                        journal_file.unlink(missing_ok=True)
                    except OSError as copy_err:
                        LOGGER.error(
                            "Crash recovery copy failed for document %s: %s. Preserving journal.",
                            doc_id,
                            copy_err,
                            exc_info=True,
                        )
                        continue
            except Exception:
                try:
                    corrupted = journal_file.with_suffix(".corrupted")
                    journal_file.replace(corrupted)
                except OSError:
                    pass

    def _load_state(self, document_id: UUID) -> DocumentState:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.*, (
                  SELECT id FROM document_versions
                  WHERE document_id = d.id
                  ORDER BY observed_at DESC, rowid DESC LIMIT 1
                ) AS version_id
                FROM documents AS d WHERE d.id = ?
                """,
                (str(document_id),),
            ).fetchone()
        if row is None:
            raise AnnotationError("document_not_found")
        if row["content_sha256"] is None or row["version_id"] is None:
            raise AnnotationError("annotation_document_not_ready")
        if str(row["format"]) != "pdf" or str(row["status"]) in {"missing", "incomplete"}:
            raise AnnotationError("annotation_document_not_writable")
        return DocumentState(
            document_id=document_id,
            library_id=UUID(str(row["library_id"])),
            path=Path(str(row["path"])),
            filename=str(row["display_name"]),
            document_version_id=UUID(str(row["version_id"])),
            content_id=str(row["content_sha256"]),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
            volume_id=str(row["volume_id"]),
            file_id=str(row["file_id"]),
            hard_link_count=int(row["hard_link_count"]),
        )

    def _verify_current_file(self, state: DocumentState) -> str:
        try:
            library = self._library_repository.get(state.library_id)
            identity = self._file_inspector.inspect_under_root(
                state.path,
                library,
            )
        except (KeyError, OSError, UnsafePath) as error:
            raise AnnotationError("annotation_external_change") from error
        expected_identity = (state.volume_id, state.file_id)
        observed_identity = (identity.volume_id, identity.file_id)
        if (
            library.status != "active"
            or identity.root_id != state.library_id
            or observed_identity != expected_identity
            or identity.hard_link_count != state.hard_link_count
            or identity.hard_link_count != 1
        ):
            raise AnnotationError("annotation_external_change")
        try:
            metadata = state.path.stat()
        except OSError as error:
            raise AnnotationError("annotation_document_not_writable") from error
        if metadata.st_size != state.size_bytes or metadata.st_mtime_ns != state.mtime_ns:
            raise AnnotationError("annotation_external_change")
        digest = self._hash_file(state.path)
        if digest != state.content_id:
            raise AnnotationError("annotation_external_change")
        return digest

    def _ensure_baseline(self, state: DocumentState, content_hash: str) -> None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM pdf_annotation_baselines WHERE document_id = ?",
                (str(state.document_id),),
            ).fetchone()
        if row is not None:
            return
        directory = self._document_backup_dir(state.document_id)
        directory.mkdir(parents=True, exist_ok=True)
        baseline = directory / "baseline.pdf"
        if not baseline.exists():
            shutil.copy2(state.path, baseline)
        if self._hash_file(baseline) != content_hash:
            raise AnnotationError("annotation_baseline_conflict")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO pdf_annotation_baselines (
                  document_id, content_sha256, backup_path, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (str(state.document_id), content_hash, str(baseline), datetime.now(UTC).isoformat()),
            )

    def _create_snapshot(self, state: DocumentState, content_hash: str) -> Path:
        directory = self._document_backup_dir(state.document_id) / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4()}.pdf"
        shutil.copy2(state.path, path)
        if self._hash_file(path) != content_hash:
            path.unlink(missing_ok=True)
            raise AnnotationError("annotation_backup_failed")
        return path

    def _record_version_and_mutation(
        self,
        previous: DocumentState,
        *,
        operation: str,
        annotation_id: str | None,
        pre_hash: str,
        post_hash: str,
        backup: Path,
    ) -> dict[str, object]:
        stat_result = previous.path.stat()
        content_id = self._hash_file(previous.path)
        version_id = uuid4()
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO contents (
                  sha256, size_bytes, mime_type, page_count, current_parse_run_id,
                  title, authors_json, status, created_at, updated_at
                ) VALUES (?, ?, 'application/pdf', NULL, NULL, NULL, NULL,
                          'candidate_pdf', ?, ?)
                """,
                (content_id, stat_result.st_size, now, now),
            )
            if inserted.rowcount == 1:
                self._queue.enqueue_in_transaction(
                    connection,
                    "parse_pdf",
                    f"parse_pdf:{content_id}",
                    {"content_id": content_id},
                )
            connection.execute(
                """
                UPDATE documents SET content_sha256 = ?, size_bytes = ?, mtime_ns = ?,
                  status = 'candidate_pdf', updated_at = ? WHERE id = ?
                """,
                (content_id, stat_result.st_size, stat_result.st_mtime_ns, now, str(previous.document_id)),
            )
            connection.execute(
                """
                INSERT INTO document_versions (
                  id, document_id, content_sha256, size_bytes, mtime_ns,
                  volume_id, file_id, hard_link_count, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(version_id), str(previous.document_id), content_id,
                    stat_result.st_size, stat_result.st_mtime_ns, previous.volume_id,
                    previous.file_id, previous.hard_link_count, now,
                ),
            )
            connection.execute(
                """
                INSERT INTO pdf_annotation_mutations (
                  id, document_id, annotation_id, operation, pre_content_sha256,
                  post_content_sha256, backup_path, created_at, undone_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(uuid4()), str(previous.document_id), annotation_id, operation,
                    pre_hash, post_hash, str(backup), now,
                ),
            )
        return {
            "document_id": str(previous.document_id),
            "current_document_version_id": str(version_id),
            "content_id": content_id,
        }

    def _record_undo_version(
        self,
        previous: DocumentState,
        mutation_id: str,
    ) -> dict[str, object]:
        stat_result = previous.path.stat()
        content_id = self._hash_file(previous.path)
        version_id = uuid4()
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO contents (
                  sha256, size_bytes, mime_type, page_count, current_parse_run_id,
                  title, authors_json, status, created_at, updated_at
                ) VALUES (?, ?, 'application/pdf', NULL, NULL, NULL, NULL,
                          'candidate_pdf', ?, ?)
                """,
                (content_id, stat_result.st_size, now, now),
            )
            if inserted.rowcount == 1:
                self._queue.enqueue_in_transaction(
                    connection,
                    "parse_pdf",
                    f"parse_pdf:{content_id}",
                    {"content_id": content_id},
                )
            connection.execute(
                """
                UPDATE documents SET content_sha256 = ?, size_bytes = ?, mtime_ns = ?,
                  status = 'candidate_pdf', updated_at = ? WHERE id = ?
                """,
                (content_id, stat_result.st_size, stat_result.st_mtime_ns, now, str(previous.document_id)),
            )
            connection.execute(
                """
                INSERT INTO document_versions (
                  id, document_id, content_sha256, size_bytes, mtime_ns,
                  volume_id, file_id, hard_link_count, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(version_id), str(previous.document_id), content_id,
                    stat_result.st_size, stat_result.st_mtime_ns, previous.volume_id,
                    previous.file_id, previous.hard_link_count, now,
                ),
            )
            connection.execute(
                "UPDATE pdf_annotation_mutations SET undone_at = ? WHERE id = ?",
                (now, mutation_id),
            )
        return {
            "document_id": str(previous.document_id),
            "current_document_version_id": str(version_id),
            "content_id": content_id,
        }

    def _safe_prune_snapshots(self, document_id: UUID) -> None:
        try:
            self._prune_snapshots(document_id)
        except Exception:
            LOGGER.warning(
                "Failed to prune snapshots for document %s",
                document_id,
                exc_info=True,
            )

    def _prune_snapshots(self, document_id: UUID) -> None:
        directory = self._document_backup_dir(document_id) / "snapshots"
        if not directory.exists():
            return
        snapshots = sorted(directory.glob("*.pdf"), key=lambda item: item.stat().st_mtime_ns)
        for path in snapshots[:-20]:
            path.unlink(missing_ok=True)

    def _current_identity(self, document_id: UUID) -> dict[str, object]:
        state = self._load_state(document_id)
        return {
            "document_id": str(document_id),
            "current_document_version_id": str(state.document_version_id),
            "content_id": state.content_id,
        }

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _document_backup_dir(self, document_id: UUID) -> Path:
        return self._backup_root / str(document_id)

    @staticmethod
    def _annotation_id(annotation: fitz.Annot) -> str:
        value = annotation.info.get("id")
        return str(value) if value else f"xref-{annotation.xref}"

    @staticmethod
    def _owned_type(annotation: fitz.Annot) -> str:
        subject = str(annotation.info.get("subject") or "")
        if subject.startswith(APP_SUBJECT_PREFIX):
            return subject.removeprefix(APP_SUBJECT_PREFIX).split(":", 1)[0]
        return "other"

    @classmethod
    def _require_owned(cls, annotation: fitz.Annot) -> None:
        if annotation.info.get("title") != APP_TITLE or cls._owned_type(annotation) == "other":
            raise AnnotationError("annotation_read_only")

    @classmethod
    def _find_annotation(
        cls, document: fitz.Document, annotation_id: str
    ) -> tuple[fitz.Page, fitz.Annot]:
        for page in document:
            for annotation in page.annots() or ():
                if cls._annotation_id(annotation) == annotation_id:
                    return page, annotation
        raise AnnotationError("annotation_not_found")

    @staticmethod
    def _pdf_quad_to_page(page: fitz.Page, values: list[float]) -> fitz.Quad:
        matrix = page.transformation_matrix
        points = [fitz.Point(values[index], values[index + 1]) * matrix for index in range(0, 8, 2)]
        quad = fitz.Quad(points)
        if not quad.is_convex or quad.is_empty:
            raise AnnotationError("annotation_quads_invalid")
        return quad

    @classmethod
    def _serialize_annotation(cls, page: fitz.Page, annotation: fitz.Annot) -> dict[str, object]:
        inverse = ~page.transformation_matrix
        vertices = annotation.vertices or []
        quads: list[list[float]] = []
        for start in range(0, len(vertices) - 3, 4):
            converted = [fitz.Point(point) * inverse for point in vertices[start : start + 4]]
            quads.append([coordinate for point in converted for coordinate in (point.x, point.y)])
        annotation_type = cls._owned_type(annotation)
        owned = annotation.info.get("title") == APP_TITLE and annotation_type != "other"
        stroke = annotation.colors.get("stroke") or []
        return {
            "annotation_id": cls._annotation_id(annotation),
            "page": page.number + 1,
            "type": annotation_type if owned else str(annotation.type[1]).lower(),
            "owned": owned,
            "read_only": not owned,
            "comment": str(annotation.info.get("content") or ""),
            "selected_text": page.get_textbox(annotation.rect).strip(),
            "subject": str(annotation.info.get("subject") or ""),
            "color": list(stroke),
            "quads": quads,
        }
