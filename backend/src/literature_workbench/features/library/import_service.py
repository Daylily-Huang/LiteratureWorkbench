from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.import_intents import (
    ImportIntentRepository,
    ImportTask,
)
from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.platform.windows.filesystem import (
    SafeFileInspector,
    SafeWindowsFileInspector,
)


MAX_BYTES = 2 * 1024**3
WRITE_SIZE = 4 * 1024**2
MOVEFILE_WRITE_THROUGH = 0x00000008
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183


class ImportService:
    def __init__(
        self,
        database: Database,
        repository: ImportIntentRepository,
        data_root: Path,
        session_generation: str,
        *,
        queue: TaskQueue | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._repository = repository
        self._data_root = Path(data_root)
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
        claimed = self._repository.claim(intent_id, self._session_generation, upload_token)
        if Path(claimed.original_name).suffix.lower() != ".pdf":
            self._repository.fail(intent_id, "invalid_pdf_extension", allowed=("receiving",))
            raise ValueError("invalid_pdf_extension")
        if declared_length is not None and declared_length != claimed.expected_size:
            self._repository.fail(intent_id, "content_length_mismatch", allowed=("receiving",))
            raise ValueError("content_length_mismatch")
        incoming = self._data_root / "imports" / ".incoming"
        part = incoming / f"{intent_id}.part"
        uploaded = incoming / f"{intent_id}.uploaded.pdf"
        written = 0
        prefix = bytearray()
        try:
            await asyncio.to_thread(incoming.mkdir, parents=True, exist_ok=True)
            handle = await asyncio.to_thread(open, part, "xb", buffering=0)
            try:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > claimed.expected_size or written > MAX_BYTES:
                        raise ValueError("upload_size_mismatch")
                    if len(prefix) < 5:
                        prefix.extend(chunk[: 5 - len(prefix)])
                    for offset in range(0, len(chunk), WRITE_SIZE):
                        await asyncio.to_thread(handle.write, chunk[offset : offset + WRITE_SIZE])
                if written != claimed.expected_size:
                    raise ValueError("upload_size_mismatch")
                if bytes(prefix) != b"%PDF-":
                    raise ValueError("invalid_pdf_signature")
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(self._move_no_replace, part, uploaded)
            with self._database.transaction() as connection:
                self._repository.mark_uploaded(intent_id, connection)
                self._queue.enqueue_in_transaction(
                    connection,
                    "validate_import",
                    f"validate_import:{intent_id}",
                    {"intent_id": str(intent_id)},
                )
        except BaseException as error:
            await asyncio.to_thread(
                self._unlink_received_file,
                part,
                intent_id,
                claimed.expected_size,
                ".part",
            )
            if not uploaded.exists():
                code = str(error) if isinstance(error, ValueError) else "upload_interrupted"
                self._repository.fail(intent_id, code, allowed=("receiving",))
            raise
        return self._repository.get(intent_id, self._session_generation)

    def _unlink_received_file(
        self,
        path: Path,
        intent_id: UUID,
        expected_size: int,
        suffix: str,
    ) -> None:
        incoming = self._data_root / "imports" / ".incoming"
        if path.parent != incoming or path.name != f"{intent_id}{suffix}":
            return
        try:
            metadata = path.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if (
                not stat.S_ISREG(metadata.st_mode)
                or attributes & 0x00000400
                or metadata.st_size > expected_size
            ):
                return
            path.unlink()
        except FileNotFoundError:
            pass

    def reconcile_stale_receiving(self) -> None:
        incoming = self._data_root / "imports" / ".incoming"
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, expected_size FROM import_intents WHERE status='receiving'"
            ).fetchall()
        for row in rows:
            intent_id = UUID(str(row["id"]))
            expected = int(row["expected_size"])
            part = incoming / f"{intent_id}.part"
            uploaded = incoming / f"{intent_id}.uploaded.pdf"
            candidate = uploaded if uploaded.exists() else part
            valid = False
            try:
                valid = (
                    candidate.is_file()
                    and candidate.stat().st_size == expected
                    and self._read_prefix(candidate) == b"%PDF-"
                )
            except OSError:
                valid = False
            if valid:
                if candidate == part:
                    self._move_no_replace(part, uploaded)
                else:
                    self._unlink_received_file(part, intent_id, expected, ".part")
                with self._database.transaction() as connection:
                    self._repository.mark_uploaded(intent_id, connection)
                    self._queue.enqueue_in_transaction(
                        connection,
                        "validate_import",
                        f"validate_import:{intent_id}",
                        {"intent_id": str(intent_id)},
                    )
            else:
                self._unlink_received_file(part, intent_id, expected, ".part")
                self._unlink_received_file(
                    uploaded, intent_id, expected, ".uploaded.pdf"
                )
                self._repository.fail(intent_id, "upload_interrupted", allowed=("receiving",))

    @staticmethod
    def _move_no_replace(source: Path, destination: Path) -> None:
        if os.name == "nt":
            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            move.restype = ctypes.c_int
            if not move(str(source), str(destination), MOVEFILE_WRITE_THROUGH):
                raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        else:
            os.link(source, destination)
            source.unlink()

    @staticmethod
    def _read_prefix(path: Path) -> bytes:
        with path.open("rb") as stream:
            return stream.read(5)


class ImportValidator:
    def __init__(
        self,
        database: Database,
        queue: TaskQueue,
        data_root: Path,
        *,
        inspector: SafeFileInspector | None = None,
    ) -> None:
        self._database = database
        self._queue = queue
        self._data_root = Path(data_root)
        self._inspector = inspector or SafeWindowsFileInspector()

    def validate_and_promote(self, intent_id: UUID) -> None:
        incoming = self._data_root / "imports" / ".incoming"
        uploaded = incoming / f"{intent_id}.uploaded.pdf"
        verify = incoming / f"{intent_id}.verify.pdf"
        try:
            with self._database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM import_intents WHERE id = ?", (str(intent_id),)
                ).fetchone()
                if row is None:
                    raise ValueError("import_not_found")
                status = str(row["status"])
                if status == "completed":
                    return
                if status not in {"uploaded", "validating", "ready_to_promote"}:
                    raise ValueError("invalid_import_state")
                if status == "ready_to_promote":
                    digest_value = row["content_sha256"]
                    final_value = row["final_path_key"]
                    if digest_value is None or final_value is None:
                        raise ValueError("promotion_recovery_unauthorized")
                    digest = str(digest_value)
                    final = Path(str(final_value))
                    display_name = str(row["file_name"])
                    ready = True
                else:
                    ready = False
                connection.execute(
                    "UPDATE import_intents SET status='validating', updated_at=? WHERE id=? AND status='uploaded'",
                    (datetime.now(UTC).isoformat(), str(intent_id)),
                )
                expected_size = int(row["expected_size"])
                display_name = str(row["file_name"])
            if ready:
                self._promote(
                    intent_id,
                    digest,
                    final,
                    os.path.normcase(os.path.abspath(final)),
                    verify,
                    display_name,
                )
                return
            validation_source = verify if status == "validating" and verify.exists() else uploaded
            digest = self._hash_and_validate(validation_source, expected_size)
            if validation_source == uploaded:
                ImportService._move_no_replace(uploaded, verify)
            year, month = self._month_parts()
            final = self._data_root / "imports" / "library" / year / month / f"{digest}.pdf"
            final_key = os.path.normcase(os.path.abspath(final))
            with self._database.transaction() as connection:
                connection.execute(
                    "UPDATE import_intents SET status='ready_to_promote', content_sha256=?, final_path_key=?, updated_at=? WHERE id=?",
                    (digest, final_key, datetime.now(UTC).isoformat(), str(intent_id)),
                )
            self._promote(intent_id, digest, final, final_key, verify, display_name)
        except ValueError as error:
            if str(error) == "promotion_owner_pending":
                return
            self._fail(intent_id, str(error), uploaded, verify)
        except OSError:
            with self._database.connect() as connection:
                state = connection.execute(
                    "SELECT status FROM import_intents WHERE id=?", (str(intent_id),)
                ).fetchone()
            if state is not None and str(state["status"]) == "ready_to_promote":
                raise
            self._fail(intent_id, "import_io_error", uploaded, verify)

    def reconcile_ready_promotions(self) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM import_intents WHERE status='ready_to_promote' ORDER BY created_at,id"
            ).fetchall()
        for row in rows:
            self.validate_and_promote(UUID(str(row["id"])))

    def _hash_and_validate(self, path: Path, expected_size: int) -> str:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            prefix = stream.read(5)
            if prefix != b"%PDF-":
                raise ValueError("invalid_pdf_signature")
            digest.update(prefix)
            size += len(prefix)
            while chunk := stream.read(4 * 1024**2):
                digest.update(chunk)
                size += len(chunk)
        if size != expected_size:
            raise ValueError("upload_size_mismatch")
        probe = (
            "import fitz,sys; d=fitz.open(sys.argv[1]); "
            "ok=(not d.needs_pass and not d.is_repaired and d.page_count>0); "
            "d.load_page(0) if ok else None; d.close(); raise SystemExit(0 if ok else 2)"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("invalid_pdf_structure") from error
        if result.returncode != 0:
            raise ValueError("invalid_pdf_structure")
        return digest.hexdigest()

    def _promote(
        self,
        intent_id: UUID,
        digest: str,
        final: Path,
        final_key: str,
        verify: Path,
        display_name: str,
    ) -> None:
        final.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._data_root / "imports" / ".locks" / f"{digest}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(lock_path))
        with lock:
            with self._database.transaction() as connection:
                promotion = connection.execute(
                    "SELECT * FROM import_promotions WHERE content_sha256=?", (digest,)
                ).fetchone()
                if promotion is None:
                    connection.execute(
                        "INSERT INTO import_promotions(content_sha256,owner_intent_id,final_path_key,status,updated_at) VALUES(?,?,?,'claimed',?)",
                        (digest, str(intent_id), final_key, datetime.now(UTC).isoformat()),
                    )
                    owner = True
                else:
                    owner = str(promotion["owner_intent_id"]) == str(intent_id)
                    if not owner and str(promotion["status"]) == "failed":
                        raise ValueError("promotion_recovery_unauthorized")
                    if not owner and str(promotion["status"]) != "completed":
                        raise ValueError("promotion_owner_pending")
            if owner:
                if verify.exists():
                    try:
                        ImportService._move_no_replace(verify, final)
                    except OSError as error:
                        if error.errno not in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS, 17}:
                            raise
                        if self._file_hash(final) != digest:
                            raise ValueError("promotion_path_conflict")
                    with self._database.transaction() as connection:
                        connection.execute(
                            "UPDATE import_promotions SET status='moved',updated_at=? WHERE content_sha256=? AND owner_intent_id=?",
                            (datetime.now(UTC).isoformat(), digest, str(intent_id)),
                        )
                elif not final.exists() or self._file_hash(final) != digest:
                    raise ValueError("promotion_recovery_unauthorized")
            elif self._file_hash(final) != digest:
                raise ValueError("promotion_recovery_unauthorized")
            self._complete(intent_id, digest, final, final_key, display_name, owner)

    def _complete(
        self,
        intent_id: UUID,
        digest: str,
        final: Path,
        final_key: str,
        display_name: str,
        owner: bool,
    ) -> None:
        managed = self.ensure_managed_import_library()
        identity = self._inspector.inspect_regular_file(final, managed)
        if identity.hard_link_count != 1:
            raise ValueError("promotion_final_unsafe")
        managed_id = str(managed.id)
        now = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT document_id FROM confirmed_imports WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
            if existing is not None:
                return
            connection.execute(
                "INSERT INTO contents(sha256,size_bytes,mime_type,status,created_at,updated_at) VALUES(?,?,'application/pdf','queued',?,?) ON CONFLICT(sha256) DO NOTHING",
                (digest, identity.size, now, now),
            )
            document = connection.execute(
                "SELECT id FROM documents WHERE path_key=?", (final_key,)
            ).fetchone()
            document_id = str(document["id"]) if document else str(uuid4())
            if document is None:
                connection.execute(
                    """
                    INSERT INTO documents(
                      id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                      mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                      auto_cloud_eligible,authorization_source,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'pdf',?,?,?,?,1,'parse',1,'confirmed_import','valid_pdf',?,?)
                    """,
                    (
                        document_id,
                        managed_id,
                        digest,
                        str(final),
                        final_key,
                        display_name,
                        identity.size,
                        identity.modified_ns,
                        identity.volume_id,
                        identity.file_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO document_versions(id,document_id,content_sha256,size_bytes,mtime_ns,volume_id,file_id,hard_link_count,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid4()), document_id, digest, identity.size, identity.modified_ns, identity.volume_id, identity.file_id, 1, now),
                )
            connection.execute(
                "INSERT INTO confirmed_imports(intent_id,document_id,content_sha256,final_path_key,confirmed_at) VALUES(?,?,?,?,?)",
                (str(intent_id), document_id, digest, final_key, now),
            )
            connection.execute(
                "UPDATE import_intents SET status='completed',error_code=NULL,updated_at=? WHERE id=?",
                (now, str(intent_id)),
            )
            if owner:
                connection.execute(
                    "UPDATE import_promotions SET status='completed',updated_at=? WHERE content_sha256=? AND owner_intent_id=?",
                    (now, digest, str(intent_id)),
                )
            self._queue.enqueue_in_transaction(
                connection, "parse_pdf", f"parse_pdf:{digest}", {"content_sha256": digest}
            )

    def ensure_managed_import_library(self) -> LibraryRoot:
        root = self._data_root / "imports" / "library"
        root.mkdir(parents=True, exist_ok=True)
        identity = self._inspector.inspect_root_directory(root)
        now = datetime.now(UTC).isoformat()
        library_id = "00000000-0000-0000-0000-000000000006"
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO libraries(id,root_path,root_path_key,root_volume_id,root_file_id,
                  recursive,discovery_mode,cloud_pdf_auto_allowed,authorization_source,status,
                  created_at,updated_at)
                VALUES(?,?,?,?,?,1,'managed_import',1,'managed_import_internal','active',?,?)
                ON CONFLICT(root_path_key) DO NOTHING
                """,
                (library_id, str(identity.canonical_path), os.path.normcase(os.path.abspath(identity.canonical_path)), identity.volume_id, identity.file_id, now, now),
            )
            row = connection.execute(
                "SELECT * FROM libraries WHERE root_path_key=?",
                (os.path.normcase(os.path.abspath(identity.canonical_path)),),
            ).fetchone()
            managed_count = connection.execute(
                "SELECT COUNT(*) FROM libraries WHERE discovery_mode='managed_import' AND status='active'"
            ).fetchone()[0]
        if row is None or managed_count != 1:
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
        root_model = LibraryRoot(
            UUID(str(row["id"])),
            identity.canonical_path,
            identity.volume_id,
            identity.file_id,
            True,
            "managed_import",
            "managed_import_internal",
            "active",
        )
        return root_model

    def _fail(self, intent_id: UUID, code: str, uploaded: Path, verify: Path, *, keep_verify: bool = False) -> None:
        for path in (uploaded, verify):
            if keep_verify and path == verify:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        ImportIntentRepository(self._database, lambda: datetime.now(UTC)).fail(
            intent_id, code, allowed=("uploaded", "validating", "ready_to_promote")
        )
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE import_promotions SET status='failed',updated_at=? WHERE owner_intent_id=? AND status<>'completed'",
                (datetime.now(UTC).isoformat(), str(intent_id)),
            )

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024**2):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _month_parts() -> tuple[str, str]:
        now = datetime.now(UTC)
        return f"{now.year:04d}", f"{now.month:02d}"
