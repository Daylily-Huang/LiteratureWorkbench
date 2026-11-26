from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.import_files import (
    MutexTimeout,
    TrustedImportFile,
    TrustedImportRoot,
    TrustedNamedMutex,
)
from literature_workbench.features.library.import_intents import (
    ImportIntentRepository,
    ImportTask,
)
from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.platform.windows.filesystem import HandleMetadata, SafeFileInspector


MAX_BYTES = 2 * 1024**3
WRITE_SIZE = 4 * 1024**2


class Cancellation(Protocol):
    def raise_if_cancelled(self) -> None: ...


class ImportPromotionPending(RuntimeError):
    pass


class ImportPromotionRejected(RuntimeError):
    pass


def _check(cancellation: Cancellation | None) -> None:
    if cancellation is not None:
        cancellation.raise_if_cancelled()


def _check_transaction(
    cancellation: Cancellation | None, connection: sqlite3.Connection
) -> None:
    _check(cancellation)
    if cancellation is None:
        return
    fence = getattr(cancellation, "require_current_lease", None)
    if callable(fence):
        fence(connection)


def _is_file_not_found(error: OSError) -> bool:
    return isinstance(error, FileNotFoundError) or getattr(error, "winerror", None) in {
        2,
        3,
    }


def _moved_identity_matches(row: sqlite3.Row, metadata: HandleMetadata) -> bool:
    return (
        int(row["cleanup_authorized"]) == 1
        and row["moved_volume_id"] is not None
        and row["moved_file_id"] is not None
        and row["moved_size_bytes"] is not None
        and row["moved_hard_link_count"] is not None
        and str(row["moved_volume_id"]) == metadata.volume_id
        and str(row["moved_file_id"]) == metadata.file_id
        and int(row["moved_size_bytes"]) == metadata.size
        and int(row["moved_hard_link_count"]) == metadata.hard_link_count
    )


def _owned_final_matches(final: TrustedImportFile, row: sqlite3.Row) -> bool:
    metadata = final.metadata()
    return (
        os.path.normcase(os.path.abspath(metadata.final_path))
        == os.path.normcase(os.path.abspath(str(row["final_path_key"])))
        and not metadata.is_reparse_point
        and metadata.hard_link_count == 1
        and _moved_identity_matches(row, metadata)
        and final.sha256() == str(row["content_sha256"])
    )


class ImportService:
    def __init__(
        self,
        database: Database,
        repository: ImportIntentRepository,
        trusted_root: TrustedImportRoot,
        session_generation: str,
        *,
        queue: TaskQueue | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(trusted_root, TrustedImportRoot):
            raise TypeError("trusted_import_root_required")
        self._database = database
        self._repository = repository
        self._root = trusted_root
        self._session_generation = session_generation
        self._queue = queue or TaskQueue(database, now)

    async def consume(
        self,
        intent_id: UUID,
        upload_token: str,
        chunks: AsyncIterator[bytes],
        *,
        declared_length: int | None = None,
    ) -> ImportTask:
        claimed = await asyncio.to_thread(
            self._repository.claim,
            intent_id,
            self._session_generation,
            upload_token,
        )
        if Path(claimed.original_name).suffix.lower() != ".pdf":
            await asyncio.to_thread(
                self._repository.fail,
                intent_id,
                "invalid_pdf_extension",
                allowed=("receiving",),
            )
            raise ValueError("invalid_pdf_extension")
        if declared_length is not None and declared_length != claimed.expected_size:
            await asyncio.to_thread(
                self._repository.fail,
                intent_id,
                "content_length_mismatch",
                allowed=("receiving",),
            )
            raise ValueError("content_length_mismatch")

        file = await asyncio.to_thread(self._root.create_incoming, intent_id)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        written = 0
        prefix = bytearray()

        async def produce() -> None:
            nonlocal written
            async for asgi_chunk in chunks:
                view = memoryview(asgi_chunk)
                for offset in range(0, len(view), WRITE_SIZE):
                    chunk = bytes(view[offset : offset + WRITE_SIZE])
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > claimed.expected_size or written > MAX_BYTES:
                        raise ValueError("upload_size_mismatch")
                    if len(prefix) < 5:
                        prefix.extend(chunk[: 5 - len(prefix)])
                    await queue.put(chunk)
            await queue.put(None)

        async def consume_queue() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    await asyncio.to_thread(file.write, item)
                finally:
                    queue.task_done()

        producer = asyncio.create_task(produce())
        consumer = asyncio.create_task(consume_queue())
        try:
            await asyncio.gather(producer, consumer)
            if written != claimed.expected_size:
                raise ValueError("upload_size_mismatch")
            if bytes(prefix) != b"%PDF-":
                raise ValueError("invalid_pdf_signature")
            await asyncio.to_thread(file.flush)
            before = await asyncio.to_thread(file.metadata)
            prefix_after_flush = await asyncio.to_thread(file.read_prefix, 5)
            if before.size != claimed.expected_size or prefix_after_flush != b"%PDF-":
                raise ValueError("upload_size_mismatch")
            await asyncio.to_thread(file.rename_incoming, intent_id, "uploaded.pdf")
            after = await asyncio.to_thread(file.metadata)
            if (after.volume_id, after.file_id) != (before.volume_id, before.file_id):
                raise ValueError("upload_identity_changed")
            await asyncio.to_thread(self._mark_uploaded_and_enqueue, intent_id)
        except BaseException as error:
            producer.cancel()
            consumer.cancel()
            try:
                await asyncio.gather(producer, consumer, return_exceptions=True)
            finally:
                try:
                    await asyncio.to_thread(file.delete)
                except OSError:
                    pass
                await asyncio.to_thread(file.close)
            code = str(error) if isinstance(error, ValueError) else "upload_interrupted"
            await asyncio.to_thread(
                self._repository.fail,
                intent_id,
                code,
                allowed=("receiving",),
            )
            raise
        await asyncio.to_thread(file.close)
        return await asyncio.to_thread(
            self._repository.get, intent_id, self._session_generation
        )

    def _mark_uploaded_and_enqueue(self, intent_id: UUID) -> None:
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents SET status='uploaded',updated_at=?
                WHERE id=? AND status='receiving' AND session_generation=?
                """,
                (now, str(intent_id), self._session_generation),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")
            self._queue.enqueue_in_transaction(
                connection,
                "validate_import",
                f"validate_import:{intent_id}",
                {"intent_id": str(intent_id)},
            )

    def reconcile_stale_receiving(self) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id,expected_size FROM import_intents
                WHERE status='receiving' AND session_generation<>?
                """,
                (self._session_generation,),
            ).fetchall()
        for row in rows:
            intent_id = UUID(str(row["id"]))
            expected = int(row["expected_size"])
            file: TrustedImportFile | None = None
            suffix = "uploaded.pdf"
            try:
                try:
                    file = self._root.open_incoming(intent_id, suffix)
                except OSError:
                    suffix = "part"
                    file = self._root.open_incoming(intent_id, suffix)
                metadata = file.metadata()
                valid = metadata.size == expected and file.read_prefix(5) == b"%PDF-"
                if not valid:
                    oversized = metadata.size > expected
                    if not oversized:
                        file.delete()
                    file.close()
                    file = None
                    self._delete_incoming_exact(
                        intent_id,
                        "part" if suffix == "uploaded.pdf" else "uploaded.pdf",
                        expected,
                    )
                    self._fail_receiving(
                        intent_id,
                        "recovery_residue_oversize" if oversized else "upload_interrupted",
                    )
                    continue
                if suffix == "part":
                    file.rename_incoming(intent_id, "uploaded.pdf")
                self._mark_stale_uploaded_and_enqueue(intent_id)
            except OSError:
                self._fail_receiving(intent_id, "upload_interrupted")
            except ValueError:
                self._fail_receiving(intent_id, "recovery_unsafe_residue")
            finally:
                if file is not None:
                    file.close()

    def sweep_failed_residue(self) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id,expected_size,content_sha256 FROM import_intents
                WHERE status='failed'
                """
            ).fetchall()
        for row in rows:
            intent_id = UUID(str(row["id"]))
            expected_size = int(row["expected_size"])
            digest = None if row["content_sha256"] is None else str(row["content_sha256"])
            for suffix in ("part", "uploaded.pdf", "verify.pdf"):
                file: TrustedImportFile | None = None
                try:
                    file = self._root.open_incoming(intent_id, suffix)
                    metadata = file.metadata()
                    if metadata.size > expected_size:
                        self._record_failed_recovery(intent_id, "recovery_residue_oversize")
                        continue
                    if suffix == "verify.pdf":
                        if digest is None:
                            self._record_failed_recovery(
                                intent_id, "recovery_verify_digest_missing"
                            )
                            continue
                        if file.sha256() != digest:
                            self._record_failed_recovery(
                                intent_id, "recovery_residue_hash_mismatch"
                            )
                            continue
                    file.delete()
                except OSError:
                    continue
                except ValueError:
                    self._record_failed_recovery(intent_id, "recovery_unsafe_residue")
                finally:
                    if file is not None:
                        file.close()

    def sweep_failed_promotions(self) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,i.id,i.expected_size
                FROM import_promotions AS p
                JOIN import_intents AS i ON i.id=p.owner_intent_id
                WHERE p.status='failed' AND p.cleanup_authorized=1
                  AND i.status='failed'
                """
            ).fetchall()
        for row in rows:
            final: TrustedImportFile | None = None
            intent_id = UUID(str(row["id"]))
            try:
                final = self._root.open_final(Path(str(row["final_path_key"])))
                if not _owned_final_matches(final, row):
                    self._record_failed_recovery(
                        intent_id, "recovery_failed_promotion_identity_mismatch"
                    )
                    continue
                final.delete()
            except OSError:
                continue
            except ValueError:
                self._record_failed_recovery(
                    intent_id, "recovery_failed_promotion_unsafe"
                )
            finally:
                if final is not None:
                    final.close()

    def _mark_stale_uploaded_and_enqueue(self, intent_id: UUID) -> None:
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents SET status='uploaded',updated_at=?
                WHERE id=? AND status='receiving' AND session_generation<>?
                """,
                (now, str(intent_id), self._session_generation),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")
            self._queue.enqueue_in_transaction(
                connection,
                "validate_import",
                f"validate_import:{intent_id}",
                {"intent_id": str(intent_id)},
            )

    def _fail_receiving(self, intent_id: UUID, error_code: str) -> None:
        self._repository.fail(
            intent_id,
            error_code,
            allowed=("receiving",),
        )

    def _record_failed_recovery(self, intent_id: UUID, error_code: str) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents SET error_code=?,updated_at=?
                WHERE id=? AND status='failed'
                """,
                (error_code, datetime.now(UTC).isoformat(), str(intent_id)),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")

    def _delete_incoming_exact(
        self, intent_id: UUID, suffix: str, expected_size: int
    ) -> None:
        file: TrustedImportFile | None = None
        try:
            file = self._root.open_incoming(intent_id, suffix)
            if file.metadata().size > expected_size:
                return
            file.delete()
        except OSError:
            return
        finally:
            if file is not None:
                file.close()


class ImportValidator:
    def __init__(
        self,
        database: Database,
        queue: TaskQueue,
        trusted_root: TrustedImportRoot,
        *,
        inspector: SafeFileInspector | None = None,
    ) -> None:
        if not isinstance(trusted_root, TrustedImportRoot):
            raise TypeError("trusted_import_root_required")
        self._database = database
        self._queue = queue
        self._root = trusted_root
        self._inspector = inspector

    def validate_and_promote(
        self,
        intent_id: UUID,
        cancellation: Cancellation | None = None,
    ) -> None:
        intent_lock = self._root.mutex(f"intent:{intent_id}")
        acquired = False
        try:
            self._acquire_lock(intent_lock, cancellation)
            acquired = True
            self._validate_locked(intent_id, cancellation)
        finally:
            if acquired:
                intent_lock.release()
            intent_lock.close()

    def _validate_locked(
        self,
        intent_id: UUID,
        cancellation: Cancellation | None,
    ) -> None:
        _check(cancellation)
        row = self._intent_row(intent_id)
        status = str(row["status"])
        if status == "completed":
            return
        if status not in {"uploaded", "validating", "ready_to_promote"}:
            raise ValueError("invalid_import_state")
        display_name = str(row["file_name"])
        expected_size = int(row["expected_size"])
        source: TrustedImportFile | None = None
        try:
            if status in {"uploaded", "validating"}:
                suffix = "uploaded.pdf" if status == "uploaded" else "verify.pdf"
                try:
                    source = self._root.open_incoming(intent_id, suffix)
                except OSError:
                    if status != "validating":
                        raise
                    source = self._root.open_incoming(intent_id, "uploaded.pdf")
                    suffix = "uploaded.pdf"
                if status == "uploaded":
                    self._cas_intent(
                        intent_id, "uploaded", "validating", cancellation
                    )
                    status = "validating"
                digest = self._validate_held(source, expected_size, cancellation)
                _check(cancellation)
                if suffix == "uploaded.pdf":
                    source.rename_incoming(intent_id, "verify.pdf")
                    _check(cancellation)
                final_path = self._promotion_path_or_current(digest)
                self._cas_ready(intent_id, digest, final_path, cancellation)
                status = "ready_to_promote"
                _check(cancellation)
            else:
                digest = str(row["content_sha256"])
                final_path = Path(str(row["final_path_key"]))
                try:
                    source = self._root.open_incoming(intent_id, "verify.pdf")
                    if self._validate_held(source, expected_size, cancellation) != digest:
                        raise ValueError("promotion_source_hash_mismatch")
                except OSError as error:
                    if not isinstance(error, FileNotFoundError) and getattr(
                        error, "winerror", None
                    ) not in {2, 3}:
                        raise
                    source = None
            self._promote(
                intent_id,
                digest,
                final_path,
                display_name,
                expected_size,
                source,
                cancellation,
            )
            source = None
        except ImportPromotionPending:
            raise
        except ImportPromotionRejected:
            return
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            if status != "ready_to_promote" and source is not None:
                try:
                    source.delete()
                except OSError:
                    pass
            self._fail_intent(intent_id, str(error), status)
        except OSError as error:
            if status == "ready_to_promote":
                if getattr(error, "winerror", None) in {80, 183}:
                    self._fail_destination_conflict(intent_id, digest)
                    return
                raise ImportPromotionPending("promotion_io_retry") from error
            if source is not None:
                try:
                    source.delete()
                except OSError:
                    pass
            self._fail_intent(intent_id, "import_io_error", status)
        finally:
            if source is not None:
                source.close()

    def _validate_held(
        self,
        file: TrustedImportFile,
        expected_size: int,
        cancellation: Cancellation | None,
    ) -> str:
        _check(cancellation)
        before = file.metadata()
        if before.size != expected_size or file.read_prefix(5) != b"%PDF-":
            raise ValueError("upload_size_mismatch")
        digest = file.sha256(cancellation)
        _check(cancellation)
        if not file.probe_pdf(timeout_seconds=120, cancellation=cancellation):
            raise ValueError("invalid_pdf_structure")
        _check(cancellation)
        after = file.metadata()
        if (
            (after.volume_id, after.file_id, after.size)
            != (before.volume_id, before.file_id, before.size)
            or after.is_reparse_point
        ):
            raise ValueError("import_identity_changed")
        return digest

    def _promote(
        self,
        intent_id: UUID,
        digest: str,
        proposed_path: Path,
        display_name: str,
        expected_size: int,
        source: TrustedImportFile | None,
        cancellation: Cancellation | None,
    ) -> None:
        content_lock = self._root.mutex(f"content:{digest}")
        acquired = False
        final: TrustedImportFile | None = None
        try:
            self._acquire_lock(content_lock, cancellation)
            acquired = True
            _check(cancellation)
            promotion = self._claim_or_read_promotion(
                intent_id,
                digest,
                proposed_path,
                None if source is None else source.metadata(),
                cancellation,
            )
            final_path = Path(str(promotion["final_path_key"]))
            owner = str(promotion["owner_intent_id"]) == str(intent_id)
            promotion_status = str(promotion["status"])
            if promotion_status == "failed" and source is not None:
                try:
                    promotion = self._takeover_failed_promotion(
                        intent_id,
                        digest,
                        final_path,
                        source.metadata(),
                        cancellation,
                    )
                except ImportPromotionRejected:
                    try:
                        source.delete()
                    except OSError:
                        pass
                    source.close()
                    source = None
                    raise
                owner = True
                promotion_status = "claimed"
            if not owner and promotion_status != "completed":
                raise ImportPromotionPending("promotion_owner_pending")
            if not owner:
                final = self._root.open_final(final_path)
                self._verify_final(final, final_path, digest, expected_size, cancellation)
                _check(cancellation)
                if source is not None:
                    source.delete()
                    source.close()
                    source = None
                try:
                    self._complete(
                        intent_id,
                        digest,
                        final_path,
                        display_name,
                        final,
                        owner=False,
                        cancellation=cancellation,
                    )
                except ValueError as error:
                    if str(error) not in {
                        "document_binding_conflict",
                        "content_binding_conflict",
                    }:
                        raise
                    self._reject_binding(
                        intent_id, digest, owner=False, error_code=str(error)
                    )
                    raise ImportPromotionRejected(str(error))
                return

            if source is not None:
                try:
                    existing = self._root.open_final(final_path)
                except OSError:
                    existing = None
                if existing is not None:
                    existing.close()
                    self._reject_preexisting_final(intent_id, digest, source)
                relative = final_path.relative_to(self._root.library_directory)
                if len(relative.parts) != 3:
                    raise ValueError("promotion_path_invalid")
                _check(cancellation)
                source.rename_final(relative.parts[0], relative.parts[1], relative.parts[2])
                _check(cancellation)
                moved_metadata = source.metadata()
                if (
                    os.path.normcase(os.path.abspath(moved_metadata.final_path))
                    != os.path.normcase(os.path.abspath(final_path))
                    or moved_metadata.is_reparse_point
                ):
                    raise ValueError("promotion_moved_identity_invalid")
                self._cas_promotion(
                    digest,
                    intent_id,
                    "claimed",
                    "moved",
                    moved_metadata,
                    cancellation,
                )
                final = source
                source = None
            else:
                if promotion_status not in {"claimed", "moved"}:
                    raise ValueError("promotion_recovery_unauthorized")
                final = self._root.open_final(final_path)
                self._verify_final(final, final_path, digest, expected_size, cancellation)
                if promotion_status == "claimed":
                    self._authorize_crash_recovery(
                        promotion, intent_id, digest, final, cancellation
                    )
                elif not _moved_identity_matches(promotion, final.metadata()):
                    raise ValueError("promotion_recovery_identity_mismatch")
            self._verify_final(final, final_path, digest, expected_size, cancellation)
            _check(cancellation)
            try:
                self._complete(
                    intent_id,
                    digest,
                    final_path,
                    display_name,
                    final,
                    owner=True,
                    cancellation=cancellation,
                )
            except ValueError as error:
                if str(error) not in {
                    "document_binding_conflict",
                    "content_binding_conflict",
                }:
                    raise
                self._reject_binding(
                    intent_id, digest, owner=True, error_code=str(error)
                )
                final.delete()
                raise ImportPromotionRejected(str(error))
        finally:
            if final is not None:
                final.close()
            if acquired:
                content_lock.release()
            content_lock.close()

    def _verify_final(
        self,
        final: TrustedImportFile,
        expected_path: Path,
        digest: str,
        expected_size: int,
        cancellation: Cancellation | None,
    ) -> None:
        metadata = final.metadata()
        if (
            os.path.normcase(os.path.abspath(metadata.final_path))
            != os.path.normcase(os.path.abspath(expected_path))
            or metadata.size != expected_size
            or metadata.hard_link_count != 1
            or metadata.is_reparse_point
        ):
            raise ValueError("promotion_final_unsafe")
        if final.sha256(cancellation) != digest:
            raise ValueError("promotion_final_hash_mismatch")
        _check(cancellation)

    def _complete(
        self,
        intent_id: UUID,
        digest: str,
        final_path: Path,
        display_name: str,
        final: TrustedImportFile,
        *,
        owner: bool,
        cancellation: Cancellation | None,
    ) -> None:
        _check(cancellation)
        managed = self.ensure_managed_import_library()
        metadata = final.metadata()
        now = datetime.now(UTC).isoformat()
        final_key = os.path.normcase(os.path.abspath(final_path))
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            current = connection.execute(
                "SELECT status,content_sha256,final_path_key FROM import_intents WHERE id=?",
                (str(intent_id),),
            ).fetchone()
            if current is None or str(current["status"]) != "ready_to_promote":
                raise ValueError("import_state_conflict")
            if (
                str(current["content_sha256"]) != digest
                or str(current["final_path_key"]) != final_key
            ):
                raise ValueError("import_binding_conflict")
            connection.execute(
                """
                INSERT INTO contents(sha256,size_bytes,mime_type,status,created_at,updated_at)
                VALUES(?,?,'application/pdf','queued',?,?) ON CONFLICT(sha256) DO NOTHING
                """,
                (digest, metadata.size, now, now),
            )
            content = connection.execute(
                "SELECT size_bytes,mime_type FROM contents WHERE sha256=?", (digest,)
            ).fetchone()
            if (
                content is None
                or int(content["size_bytes"]) != metadata.size
                or str(content["mime_type"]) != "application/pdf"
            ):
                raise ValueError("content_binding_conflict")
            document = connection.execute(
                "SELECT * FROM documents WHERE path_key=?", (final_key,)
            ).fetchone()
            document_id = str(document["id"]) if document else str(uuid4())
            if document is not None:
                confirmation = connection.execute(
                    """
                    SELECT 1 FROM confirmed_imports AS ci
                    JOIN import_intents AS ii ON ii.id=ci.intent_id
                    WHERE ci.document_id=? AND ci.content_sha256=?
                      AND ci.final_path_key=? AND ii.status='completed'
                      AND ii.content_sha256=ci.content_sha256
                      AND ii.final_path_key=ci.final_path_key
                    LIMIT 1
                    """,
                    (document_id, digest, final_key),
                ).fetchone()
                version = connection.execute(
                    """
                    SELECT * FROM document_versions WHERE document_id=?
                    ORDER BY observed_at DESC,id DESC LIMIT 1
                    """,
                    (document_id,),
                ).fetchone()
                if (
                    str(document["library_id"]) != str(managed.id)
                    or str(document["content_sha256"]) != digest
                    or os.path.normcase(os.path.abspath(str(document["path"]))) != final_key
                    or str(document["format"]) != "pdf"
                    or str(document["status"]) != "valid_pdf"
                    or int(document["size_bytes"]) != metadata.size
                    or str(document["volume_id"]) != metadata.volume_id
                    or str(document["file_id"]) != metadata.file_id
                    or int(document["hard_link_count"]) != metadata.hard_link_count
                    or str(document["authorization_source"]) != "confirmed_import"
                    or confirmation is None
                    or version is None
                    or str(version["content_sha256"]) != digest
                    or int(version["size_bytes"]) != metadata.size
                    or str(version["volume_id"]) != metadata.volume_id
                    or str(version["file_id"]) != metadata.file_id
                    or int(version["hard_link_count"]) != metadata.hard_link_count
                ):
                    raise ValueError("document_binding_conflict")
                # A fresh confirmed import revalidates the file and its binding.
                # Repair eligibility lost by older parser versions only after that proof.
                connection.execute(
                    "UPDATE documents SET auto_cloud_eligible=1,updated_at=? WHERE id=?",
                    (now, document_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO documents(
                      id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                      mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                      auto_cloud_eligible,authorization_source,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'pdf',?,?,?,?,?,'parse',1,'confirmed_import',
                             'valid_pdf',?,?)
                    """,
                    (
                        document_id,
                        str(managed.id),
                        digest,
                        str(metadata.final_path),
                        final_key,
                        display_name,
                        metadata.size,
                        metadata.modified_ns,
                        metadata.volume_id,
                        metadata.file_id,
                        metadata.hard_link_count,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO document_versions(
                      id,document_id,content_sha256,size_bytes,mtime_ns,volume_id,file_id,
                      hard_link_count,observed_at) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid4()),
                        document_id,
                        digest,
                        metadata.size,
                        metadata.modified_ns,
                        metadata.volume_id,
                        metadata.file_id,
                        metadata.hard_link_count,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO confirmed_imports(
                  intent_id,document_id,content_sha256,final_path_key,confirmed_at)
                VALUES(?,?,?,?,?)
                """,
                (str(intent_id), document_id, digest, final_key, now),
            )
            changed = connection.execute(
                """
                UPDATE import_intents SET status='completed',error_code=NULL,updated_at=?
                WHERE id=? AND status='ready_to_promote' AND content_sha256=?
                  AND final_path_key=?
                """,
                (now, str(intent_id), digest, final_key),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")
            if owner:
                changed = connection.execute(
                    """
                    UPDATE import_promotions SET status='completed',updated_at=?
                    WHERE content_sha256=? AND owner_intent_id=? AND status IN ('claimed','moved')
                    """,
                    (now, digest, str(intent_id)),
                )
                if changed.rowcount != 1:
                    raise ValueError("promotion_state_conflict")
            self._queue.enqueue_in_transaction(
                connection,
                "parse_pdf",
                f"parse_pdf:{digest}",
                {"content_sha256": digest},
            )
        _check(cancellation)

    def reconcile_ready_promotions(
        self, cancellation: Cancellation | None = None
    ) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM import_intents
                WHERE status='ready_to_promote' ORDER BY created_at,id
                """
            ).fetchall()
        for row in rows:
            _check(cancellation)
            try:
                self.validate_and_promote(UUID(str(row["id"])), cancellation)
            except ImportPromotionPending:
                continue

    def ensure_managed_import_library(self) -> LibraryRoot:
        identity = (
            self._inspector.inspect_root_directory(self._root.library_directory)
            if self._inspector is not None
            else None
        )
        if identity is None:
            metadata = self._root._library_handle.metadata
            from literature_workbench.platform.windows.filesystem import RootIdentity

            identity = RootIdentity(
                metadata.final_path, metadata.volume_id, metadata.file_id
            )
        now = datetime.now(UTC).isoformat()
        library_id = "00000000-0000-0000-0000-000000000006"
        path_key = os.path.normcase(os.path.abspath(identity.canonical_path))
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO libraries(
                  id,root_path,root_path_key,root_volume_id,root_file_id,recursive,
                  discovery_mode,cloud_pdf_auto_allowed,authorization_source,status,
                  created_at,updated_at)
                VALUES(?,?,?,?,?,1,'managed_import',1,'managed_import_internal','active',?,?)
                ON CONFLICT(root_path_key) DO NOTHING
                """,
                (
                    library_id,
                    str(identity.canonical_path),
                    path_key,
                    identity.volume_id,
                    identity.file_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM libraries WHERE root_path_key=?", (path_key,)
            ).fetchone()
            count = connection.execute(
                """
                SELECT COUNT(*) FROM libraries
                WHERE discovery_mode='managed_import' AND status='active'
                """
            ).fetchone()[0]
        if row is None or count != 1:
            raise ValueError("managed_import_library_conflict")
        if (
            str(row["root_volume_id"]) != identity.volume_id
            or str(row["root_file_id"]) != identity.file_id
            or str(row["discovery_mode"]) != "managed_import"
            or str(row["authorization_source"]) != "managed_import_internal"
            or int(row["cloud_pdf_auto_allowed"]) != 1
            or str(row["status"]) != "active"
        ):
            raise ValueError("managed_import_library_conflict")
        return LibraryRoot(
            UUID(str(row["id"])),
            identity.canonical_path,
            identity.volume_id,
            identity.file_id,
            True,
            "managed_import",
            "managed_import_internal",
            "active",
        )

    def _intent_row(self, intent_id: UUID) -> sqlite3.Row:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM import_intents WHERE id=?", (str(intent_id),)
            ).fetchone()
        if row is None:
            raise ValueError("import_not_found")
        return cast(sqlite3.Row, row)

    def _cas_intent(
        self,
        intent_id: UUID,
        expected: str,
        target: str,
        cancellation: Cancellation | None,
    ) -> None:
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            changed = connection.execute(
                "UPDATE import_intents SET status=?,updated_at=? WHERE id=? AND status=?",
                (target, datetime.now(UTC).isoformat(), str(intent_id), expected),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")

    def _cas_ready(
        self,
        intent_id: UUID,
        digest: str,
        final_path: Path,
        cancellation: Cancellation | None,
    ) -> None:
        final_key = os.path.normcase(os.path.abspath(final_path))
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            changed = connection.execute(
                """
                UPDATE import_intents SET status='ready_to_promote',content_sha256=?,
                  final_path_key=?,updated_at=? WHERE id=? AND status='validating'
                """,
                (digest, final_key, datetime.now(UTC).isoformat(), str(intent_id)),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")

    def _promotion_path_or_current(self, digest: str) -> Path:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT final_path_key FROM import_promotions WHERE content_sha256=?",
                (digest,),
            ).fetchone()
        if row is not None:
            return Path(str(row["final_path_key"]))
        now = datetime.now(UTC)
        return (
            self._root.library_directory
            / f"{now.year:04d}"
            / f"{now.month:02d}"
            / f"{digest}.pdf"
        )

    def _claim_or_read_promotion(
        self,
        intent_id: UUID,
        digest: str,
        final_path: Path,
        source_metadata: HandleMetadata | None,
        cancellation: Cancellation | None,
    ) -> sqlite3.Row:
        final_key = os.path.normcase(os.path.abspath(final_path))
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            row = connection.execute(
                "SELECT * FROM import_promotions WHERE content_sha256=?", (digest,)
            ).fetchone()
            if row is None:
                if source_metadata is None:
                    raise ValueError("promotion_source_identity_missing")
                connection.execute(
                    """
                    INSERT INTO import_promotions(
                      content_sha256,owner_intent_id,final_path_key,status,updated_at,
                      expected_volume_id,expected_file_id,expected_size_bytes,
                      expected_hard_link_count,cleanup_authorized,ownership_provenance)
                    VALUES(?,?,?,'claimed',?,?,?,?,?,0,'source_claim')
                    """,
                    (
                        digest,
                        str(intent_id),
                        final_key,
                        now,
                        source_metadata.volume_id,
                        source_metadata.file_id,
                        source_metadata.size,
                        source_metadata.hard_link_count,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM import_promotions WHERE content_sha256=?", (digest,)
                ).fetchone()
        if row is None:
            raise ValueError("promotion_claim_failed")
        return cast(sqlite3.Row, row)

    def _cas_promotion(
        self,
        digest: str,
        intent_id: UUID,
        expected: str,
        target: str,
        moved_metadata: HandleMetadata,
        cancellation: Cancellation | None,
    ) -> None:
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            changed = connection.execute(
                """
                UPDATE import_promotions SET status=?,updated_at=?,
                  moved_volume_id=?,moved_file_id=?,moved_size_bytes=?,
                  moved_hard_link_count=?,cleanup_authorized=1,
                  ownership_provenance='handle_move'
                WHERE content_sha256=? AND owner_intent_id=? AND status=?
                  AND expected_volume_id=? AND expected_file_id=?
                  AND expected_size_bytes=? AND expected_hard_link_count=?
                  AND cleanup_authorized=0
                """,
                (
                    target,
                    datetime.now(UTC).isoformat(),
                    moved_metadata.volume_id,
                    moved_metadata.file_id,
                    moved_metadata.size,
                    moved_metadata.hard_link_count,
                    digest,
                    str(intent_id),
                    expected,
                    moved_metadata.volume_id,
                    moved_metadata.file_id,
                    moved_metadata.size,
                    moved_metadata.hard_link_count,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("promotion_state_conflict")

    def _reject_preexisting_final(
        self,
        intent_id: UUID,
        digest: str,
        source: TrustedImportFile,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents SET status='failed',error_code='promotion_path_conflict',
                  updated_at=? WHERE id=? AND status='ready_to_promote'
                  AND content_sha256=?
                """,
                (now, str(intent_id), digest),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")
            changed = connection.execute(
                """
                UPDATE import_promotions SET status='failed',cleanup_authorized=0,
                  ownership_provenance='source_claim',updated_at=?
                WHERE content_sha256=? AND owner_intent_id=? AND status='claimed'
                """,
                (now, digest, str(intent_id)),
            )
            if changed.rowcount != 1:
                raise ValueError("promotion_state_conflict")
        source.delete()
        source.close()
        raise ImportPromotionRejected("promotion_path_conflict")

    def _reject_binding(
        self,
        intent_id: UUID,
        digest: str,
        *,
        owner: bool,
        error_code: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents SET status='failed',error_code=?,
                  updated_at=? WHERE id=? AND status='ready_to_promote'
                  AND content_sha256=?
                """,
                (error_code, now, str(intent_id), digest),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")
            if owner:
                changed = connection.execute(
                    """
                    UPDATE import_promotions SET status='failed',updated_at=?
                    WHERE content_sha256=? AND owner_intent_id=? AND status='moved'
                    """,
                    (now, digest, str(intent_id)),
                )
                if changed.rowcount != 1:
                    raise ValueError("promotion_state_conflict")

    def _fail_destination_conflict(self, intent_id: UUID, digest: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents SET status='failed',error_code='promotion_path_conflict',
                  updated_at=? WHERE id=? AND status='ready_to_promote'
                  AND content_sha256=?
                """,
                (now, str(intent_id), digest),
            )
            if changed.rowcount != 1:
                raise ImportPromotionPending("promotion_conflict_state_retry")
            changed = connection.execute(
                """
                UPDATE import_promotions SET status='failed',cleanup_authorized=0,
                  ownership_provenance='source_claim',updated_at=?
                WHERE content_sha256=? AND owner_intent_id=? AND status='claimed'
                """,
                (now, digest, str(intent_id)),
            )
            if changed.rowcount != 1:
                raise ImportPromotionPending("promotion_conflict_state_retry")

    def _fail_intent(self, intent_id: UUID, error_code: str, current: str) -> None:
        persisted_error = (
            f"recovery_{error_code}" if current == "ready_to_promote" else error_code
        )
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT content_sha256 FROM import_intents WHERE id=? AND status=?",
                (str(intent_id), current),
            ).fetchone()
            changed = connection.execute(
                """
                UPDATE import_intents SET status='failed',error_code=?,updated_at=?
                WHERE id=? AND status=?
                """,
                (
                    persisted_error,
                    datetime.now(UTC).isoformat(),
                    str(intent_id),
                    current,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")
            if current == "ready_to_promote" and row is not None:
                digest = row["content_sha256"]
                if digest is not None:
                    connection.execute(
                        """
                        UPDATE import_promotions SET status='failed',updated_at=?
                        WHERE content_sha256=? AND owner_intent_id=?
                          AND status IN ('claimed','moved')
                        """,
                        (
                            datetime.now(UTC).isoformat(),
                            str(digest),
                            str(intent_id),
                        ),
                    )

    def _takeover_failed_promotion(
        self,
        intent_id: UUID,
        digest: str,
        final_path: Path,
        source_metadata: HandleMetadata,
        cancellation: Cancellation | None,
    ) -> sqlite3.Row:
        with self._database.connect() as connection:
            promotion = connection.execute(
                "SELECT * FROM import_promotions WHERE content_sha256=? AND status='failed'",
                (digest,),
            ).fetchone()
        if promotion is None:
            raise ImportPromotionPending("promotion_takeover_retry")
        failed_final: TrustedImportFile | None = None
        if int(promotion["cleanup_authorized"]) == 1:
            try:
                failed_final = self._root.open_final(final_path)
                if not _owned_final_matches(failed_final, promotion):
                    self._reject_owned_identity_conflict(
                        intent_id,
                        digest,
                        cancellation,
                    )
                    raise ImportPromotionRejected(
                        "promotion_owned_identity_conflict"
                    )
                _check(cancellation)
                try:
                    failed_final.delete()
                except OSError as error:
                    raise ImportPromotionPending(
                        "promotion_cleanup_io_retry"
                    ) from error
            except OSError as error:
                if not _is_file_not_found(error):
                    raise ImportPromotionPending(
                        "promotion_cleanup_io_retry"
                    ) from error
            finally:
                if failed_final is not None:
                    failed_final.close()
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            changed = connection.execute(
                """
                UPDATE import_promotions SET owner_intent_id=?,status='claimed',updated_at=?,
                  expected_volume_id=?,expected_file_id=?,expected_size_bytes=?,
                  expected_hard_link_count=?,moved_volume_id=NULL,moved_file_id=NULL,
                  moved_size_bytes=NULL,moved_hard_link_count=NULL,cleanup_authorized=0,
                  ownership_provenance='source_claim'
                WHERE content_sha256=? AND status='failed'
                """,
                (
                    str(intent_id),
                    datetime.now(UTC).isoformat(),
                    source_metadata.volume_id,
                    source_metadata.file_id,
                    source_metadata.size,
                    source_metadata.hard_link_count,
                    digest,
                ),
            )
            if changed.rowcount != 1:
                raise ImportPromotionPending("promotion_takeover_retry")
            row = connection.execute(
                "SELECT * FROM import_promotions WHERE content_sha256=?", (digest,)
            ).fetchone()
        if row is None:
            raise ImportPromotionPending("promotion_takeover_retry")
        return cast(sqlite3.Row, row)

    def _reject_owned_identity_conflict(
        self,
        intent_id: UUID,
        digest: str,
        cancellation: Cancellation | None,
    ) -> None:
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            changed = connection.execute(
                """
                UPDATE import_intents SET status='failed',error_code=?,updated_at=?
                WHERE id=? AND status='ready_to_promote' AND content_sha256=?
                """,
                (
                    "promotion_owned_identity_conflict",
                    datetime.now(UTC).isoformat(),
                    str(intent_id),
                    digest,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("import_state_conflict")

    def _authorize_crash_recovery(
        self,
        promotion: sqlite3.Row,
        intent_id: UUID,
        digest: str,
        final: TrustedImportFile,
        cancellation: Cancellation | None,
    ) -> None:
        metadata = final.metadata()
        if not self._expected_identity_matches(promotion, metadata):
            raise ValueError("promotion_recovery_identity_mismatch")
        with self._database.transaction() as connection:
            _check_transaction(cancellation, connection)
            changed = connection.execute(
                """
                UPDATE import_promotions SET status='moved',updated_at=?,
                  moved_volume_id=?,moved_file_id=?,moved_size_bytes=?,
                  moved_hard_link_count=?,cleanup_authorized=1,
                  ownership_provenance='crash_recovery'
                WHERE content_sha256=? AND owner_intent_id=? AND status='claimed'
                  AND cleanup_authorized=0 AND expected_volume_id=?
                  AND expected_file_id=? AND expected_size_bytes=?
                  AND expected_hard_link_count=?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    metadata.volume_id,
                    metadata.file_id,
                    metadata.size,
                    metadata.hard_link_count,
                    digest,
                    str(intent_id),
                    metadata.volume_id,
                    metadata.file_id,
                    metadata.size,
                    metadata.hard_link_count,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("promotion_state_conflict")

    @staticmethod
    def _expected_identity_matches(row: sqlite3.Row, metadata: HandleMetadata) -> bool:
        return (
            row["expected_volume_id"] is not None
            and row["expected_file_id"] is not None
            and row["expected_size_bytes"] is not None
            and row["expected_hard_link_count"] is not None
            and str(row["expected_volume_id"]) == metadata.volume_id
            and str(row["expected_file_id"]) == metadata.file_id
            and int(row["expected_size_bytes"]) == metadata.size
            and int(row["expected_hard_link_count"]) == metadata.hard_link_count
        )

    @staticmethod
    def _acquire_lock(
        lock: TrustedNamedMutex, cancellation: Cancellation | None
    ) -> None:
        while True:
            _check(cancellation)
            try:
                lock.acquire(timeout=0.1)
                return
            except MutexTimeout:
                continue
