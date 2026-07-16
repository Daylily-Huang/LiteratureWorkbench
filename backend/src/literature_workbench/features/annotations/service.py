from __future__ import annotations

import hashlib
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
    def __init__(self, database: Database, data_root: Path, queue: TaskQueue) -> None:
        self._database = database
        self._backup_root = data_root / ".annotation-backups"
        self._queue = queue
        self._locks = _DocumentLocks()

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
            backup = Path(str(row["backup_path"]))
            if not backup.is_file():
                raise AnnotationError("annotation_backup_missing")
            shutil.copy2(backup, state.path)
            restored_hash = self._hash_file(state.path)
            if restored_hash != str(row["pre_content_sha256"]):
                raise AnnotationError("annotation_restore_failed")
            identity = self._record_new_version(state)
            with self._database.transaction() as connection:
                connection.execute(
                    "UPDATE pdf_annotation_mutations SET undone_at = ? WHERE id = ?",
                    (datetime.now(UTC).isoformat(), str(row["id"])),
                )
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
            shutil.copy2(baseline, state.path)
            post_hash = self._hash_file(state.path)
            if post_hash != str(row["content_sha256"]):
                shutil.copy2(backup, state.path)
                raise AnnotationError("annotation_restore_failed")
            identity = self._record_new_version(state)
            self._record_mutation(
                state,
                operation="restore",
                annotation_id=None,
                pre_hash=pre_hash,
                post_hash=post_hash,
                backup=backup,
            )
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
                identity = self._record_new_version(state)
                self._record_mutation(
                    state,
                    operation=operation,
                    annotation_id=annotation_id,
                    pre_hash=pre_hash,
                    post_hash=post_hash,
                    backup=backup,
                )
                self._prune_snapshots(state.document_id)
                return {"annotation_id": annotation_id, **identity}
            except BaseException:
                if self._hash_file(state.path) != pre_hash:
                    shutil.copy2(backup, state.path)
                raise

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

    def _record_new_version(self, previous: DocumentState) -> dict[str, object]:
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
        return {
            "document_id": str(previous.document_id),
            "current_document_version_id": str(version_id),
            "content_id": content_id,
        }

    def _record_mutation(
        self,
        state: DocumentState,
        *,
        operation: str,
        annotation_id: str | None,
        pre_hash: str,
        post_hash: str,
        backup: Path,
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO pdf_annotation_mutations (
                  id, document_id, annotation_id, operation, pre_content_sha256,
                  post_content_sha256, backup_path, created_at, undone_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(uuid4()), str(state.document_id), annotation_id, operation,
                    pre_hash, post_hash, str(backup), datetime.now(UTC).isoformat(),
                ),
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
