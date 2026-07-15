from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
import sys

import fitz
import pytest

from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.library.import_intents import ImportIntentRepository
from literature_workbench.features.library.import_service import (
    ImportPromotionPending,
    ImportService,
    ImportValidator,
)
from literature_workbench.features.library.import_files import TrustedImportRoot
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.worker import Worker


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database.open(tmp_path / "unit.sqlite3")
    run_migrations(value)
    return value


def _valid_pdf() -> bytes:
    document = fitz.open()
    document.new_page()
    data = document.tobytes()
    document.close()
    return data


def _now() -> datetime:
    return datetime.now(UTC)


def _trusted(path: Path) -> TrustedImportRoot:
    return TrustedImportRoot.create(path)


async def _chunks(data: bytes):
    yield data


@pytest.mark.parametrize(
    ("name", "body", "error"),
    [
        ("paper.docx", b"%PDF-1.7", "invalid_pdf_extension"),
        ("paper.pdf", b"not a pdf", "invalid_pdf_signature"),
    ],
)
def test_prejournal_rejections_leave_no_bytes_documents_or_jobs(
    tmp_path: Path, database, name: str, body: bytes, error: str
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    created = repository.create("generation-a", name, len(body), "application/pdf")
    service = ImportService(
        database,
        repository,
        _trusted(tmp_path),
        session_generation="generation-a",
        now=now,
    )

    with pytest.raises(ValueError, match=error):
        asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))

    incoming = tmp_path / "imports" / ".incoming"
    assert list(incoming.iterdir()) == [] if incoming.exists() else True
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_declared_size_mismatch_cleans_exact_part(tmp_path: Path, database) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    created = repository.create("generation-a", "paper.pdf", 99, "application/pdf")
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", now=now)

    with pytest.raises(ValueError, match="upload_size_mismatch"):
        asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(b"%PDF-1.7")))

    assert not (tmp_path / "imports" / ".incoming" / f"{created.intent_id}.part").exists()


def test_worker_rejects_malformed_pdf_and_cleans_journaled_file(tmp_path: Path, database) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = b"%PDF-1.7\nnot structurally readable\n%%EOF"
    created = repository.create("generation-a", "bad.pdf", len(body), "application/pdf")
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", now=now)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, TaskQueue(database, now), _trusted(tmp_path))

    validator.validate_and_promote(created.intent_id)

    view = repository.get(created.intent_id, "generation-a")
    assert view.status == "failed"
    assert view.error_code == "invalid_pdf_structure"
    assert list((tmp_path / "imports" / ".incoming").iterdir()) == []
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0


def test_worker_rejects_pdf_that_requires_xref_repair(tmp_path: Path, database) -> None:
    body = _valid_pdf()
    start = body.rfind(b"startxref\n") + len(b"startxref\n")
    end = body.find(b"\n", start)
    repaired = body[:start] + b"999999999" + body[end:]
    repository = ImportIntentRepository(database, _now)
    created = repository.create(
        "generation-a", "repaired.pdf", len(repaired), "application/pdf"
    )
    queue = TaskQueue(database, _now)
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(repaired)))

    ImportValidator(database, queue, _trusted(tmp_path)).validate_and_promote(created.intent_id)

    view = repository.get(created.intent_id, "generation-a")
    assert (view.status, view.error_code) == ("failed", "invalid_pdf_structure")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0


def test_valid_pdf_is_promoted_without_replace_and_authorized_only_on_completion(
    tmp_path: Path, database
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = _valid_pdf()
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    queue = TaskQueue(database, now)
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue, now=now)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, _trusted(tmp_path))

    validator.validate_and_promote(created.intent_id)
    # Simulate a crash after the completion transaction but before worker job ack.
    validator.validate_and_promote(created.intent_id)

    view = repository.get(created.intent_id, "generation-a")
    assert view.status == "completed"
    assert view.document_id is not None
    with database.connect() as connection:
        document = connection.execute(
            "SELECT authorization_source, auto_cloud_eligible FROM documents WHERE id = ?",
            (str(view.document_id),),
        ).fetchone()
        confirmation = connection.execute(
            "SELECT intent_id FROM confirmed_imports WHERE intent_id = ?",
            (str(created.intent_id),),
        ).fetchone()
        parse_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind = 'parse_pdf'"
        ).fetchone()[0]
        versions = connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0]
        promotions = connection.execute("SELECT COUNT(*) FROM import_promotions").fetchone()[0]
    assert tuple(document) == ("confirmed_import", 1)
    assert confirmation["intent_id"] == str(created.intent_id)
    assert parse_jobs == 1
    assert versions == 1
    assert promotions == 1


def test_preexisting_different_hash_path_is_never_replaced_or_authorized(
    tmp_path: Path, database
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = _valid_pdf()
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    queue = TaskQueue(database, now)
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue, now=now)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, _trusted(tmp_path))
    digest = __import__("hashlib").sha256(body).hexdigest()
    current = datetime.now(UTC)
    final = (
        tmp_path
        / "imports"
        / "library"
        / f"{current.year:04d}"
        / f"{current.month:02d}"
        / f"{digest}.pdf"
    )
    final.parent.mkdir(parents=True)
    final.write_bytes(b"different")
    validator.validate_and_promote(created.intent_id)

    assert final.read_bytes() == b"different"
    assert repository.get(created.intent_id, "generation-a").status == "failed"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0


def test_preexisting_same_hash_final_with_source_present_is_rejected(
    tmp_path: Path, database
) -> None:
    body = _valid_pdf()
    repository = ImportIntentRepository(database, _now)
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    queue = TaskQueue(database, _now)
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    digest = __import__("hashlib").sha256(body).hexdigest()
    current = datetime.now(UTC)
    final = tmp_path / "imports" / "library" / f"{current.year:04d}" / f"{current.month:02d}" / f"{digest}.pdf"
    final.parent.mkdir(parents=True)
    final.write_bytes(body)

    ImportValidator(database, queue, _trusted(tmp_path)).validate_and_promote(created.intent_id)

    assert final.read_bytes() == body
    assert repository.get(created.intent_id, "generation-a").status == "failed"
    assert not (tmp_path / "imports" / ".incoming" / f"{created.intent_id}.verify.pdf").exists()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0


def test_worker_rejects_encrypted_pdf(tmp_path: Path, database) -> None:
    document = fitz.open()
    document.new_page()
    body = document.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    document.close()
    repository = ImportIntentRepository(database, _now)
    created = repository.create("generation-a", "encrypted.pdf", len(body), "application/pdf")
    queue = TaskQueue(database, _now)
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))

    ImportValidator(database, queue, _trusted(tmp_path)).validate_and_promote(created.intent_id)

    view = repository.get(created.intent_id, "generation-a")
    assert (view.status, view.error_code) == ("failed", "invalid_pdf_structure")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0


def test_supervisor_recovers_full_receiving_part_but_worker_restart_does_not_touch_it(
    tmp_path: Path, database
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = _valid_pdf()
    created = repository.create("prior-generation", "paper.pdf", len(body), "application/pdf")
    repository.claim(created.intent_id, "prior-generation", created.upload_token)
    part = tmp_path / "imports" / ".incoming" / f"{created.intent_id}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(body)
    queue = TaskQueue(database, now)

    # Constructing/restarting only worker-side validation never reconciles receiving bytes.
    ImportValidator(database, queue, _trusted(tmp_path))
    assert part.read_bytes() == body
    assert repository.get(created.intent_id, "prior-generation").status == "receiving"

    ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue).reconcile_stale_receiving()

    assert not part.exists()
    assert (part.parent / f"{created.intent_id}.uploaded.pdf").exists()
    assert repository.get(created.intent_id, "prior-generation").status == "uploaded"
    assert queue.count(kind="validate_import") == 1


def test_supervisor_never_touches_current_generation_receiving_file(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, _now)
    body = _valid_pdf()
    created = repository.create("generation-a", "live.pdf", len(body), "application/pdf")
    repository.claim(created.intent_id, "generation-a", created.upload_token)
    part = tmp_path / "imports" / ".incoming" / f"{created.intent_id}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(body)

    ImportService(database, repository, _trusted(tmp_path), "generation-a").reconcile_stale_receiving()

    assert part.read_bytes() == body
    assert repository.get(created.intent_id, "generation-a").status == "receiving"


@pytest.mark.parametrize(
    ("phase", "expected_status", "expected_file", "expected_jobs"),
    [
        ("before_fsync", "failed", None, 0),
        ("after_fsync", "uploaded", "uploaded.pdf", 1),
        ("after_rename", "uploaded", "uploaded.pdf", 1),
        ("after_enqueue", "uploaded", "uploaded.pdf", 1),
    ],
)
def test_receive_crash_recovery_matrix(
    tmp_path: Path,
    database,
    phase: str,
    expected_status: str,
    expected_file: str | None,
    expected_jobs: int,
) -> None:
    repository = ImportIntentRepository(database, _now)
    queue = TaskQueue(database, _now)
    body = _valid_pdf()
    created = repository.create("prior-generation", "paper.pdf", len(body), "application/pdf")
    repository.claim(created.intent_id, "prior-generation", created.upload_token)
    trusted = _trusted(tmp_path)
    live = trusted.create_incoming(created.intent_id)
    live.write(body if phase != "before_fsync" else body[: max(5, len(body) // 2)])
    if phase != "before_fsync":
        live.flush()
    if phase in {"after_rename", "after_enqueue"}:
        live.rename_incoming(created.intent_id, "uploaded.pdf")
    live.close()
    if phase == "after_enqueue":
        repository.mark_uploaded(created.intent_id)
        queue.enqueue(
            "validate_import",
            f"validate_import:{created.intent_id}",
            {"intent_id": str(created.intent_id)},
        )

    ImportService(
        database,
        repository,
        trusted,
        "current-generation",
        queue=queue,
    ).reconcile_stale_receiving()

    assert repository.get(created.intent_id, "prior-generation").status == expected_status
    assert queue.count(kind="validate_import") == expected_jobs
    assert not trusted.incoming_path(created.intent_id, "part").exists()
    uploaded = trusted.incoming_path(created.intent_id, "uploaded.pdf")
    assert uploaded.exists() is (expected_file == "uploaded.pdf")
    trusted.close()


def test_worker_only_restart_does_not_touch_live_writer(tmp_path: Path, database) -> None:
    repository = ImportIntentRepository(database, _now)
    queue = TaskQueue(database, _now)
    body = _valid_pdf()
    created = repository.create("generation-a", "live.pdf", len(body), "application/pdf")
    repository.claim(created.intent_id, "generation-a", created.upload_token)
    trusted = _trusted(tmp_path)
    live = trusted.create_incoming(created.intent_id)
    split = len(body) // 2
    live.write(body[:split])

    ImportValidator(database, queue, trusted)
    Worker(queue, "restarted-worker", {}, lease_seconds=30)
    live.write(body[split:])
    live.flush()

    assert live.metadata().size == len(body)
    assert live.sha256() == __import__("hashlib").sha256(body).hexdigest()
    assert repository.get(created.intent_id, "generation-a").status == "receiving"
    live.close()
    trusted.close()


def test_supervisor_cleans_only_exact_invalid_receiving_files(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, _now)
    created = repository.create("prior-generation", "paper.pdf", 9, "application/pdf")
    repository.claim(created.intent_id, "prior-generation", created.upload_token)
    incoming = tmp_path / "imports" / ".incoming"
    incoming.mkdir(parents=True)
    uploaded = incoming / f"{created.intent_id}.uploaded.pdf"
    part = incoming / f"{created.intent_id}.part"
    unrelated = incoming / "unrelated.part"
    uploaded.write_bytes(b"not-a-pdf")
    part.write_bytes(b"partial")
    unrelated.write_bytes(b"keep")

    ImportService(database, repository, _trusted(tmp_path), "generation-a").reconcile_stale_receiving()

    assert repository.get(created.intent_id, "prior-generation").status == "failed"
    assert not uploaded.exists()
    assert not part.exists()
    assert unrelated.read_bytes() == b"keep"


def test_move_before_database_completion_is_recovered_only_by_recorded_owner(
    tmp_path: Path, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = _valid_pdf()
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    queue = TaskQueue(database, now)
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, _trusted(tmp_path))
    original_complete = validator._complete

    def crash(*args, **kwargs):
        raise RuntimeError("crash_after_move")

    monkeypatch.setattr(validator, "_complete", crash)
    with pytest.raises(RuntimeError, match="crash_after_move"):
        validator.validate_and_promote(created.intent_id)
    with database.connect() as connection:
        state = connection.execute(
            "SELECT status FROM import_intents WHERE id=?", (str(created.intent_id),)
        ).fetchone()[0]
        promotion = connection.execute(
            "SELECT owner_intent_id,status FROM import_promotions"
        ).fetchone()
        confirmations = connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0]
    assert state == "ready_to_promote"
    assert tuple(promotion) == (str(created.intent_id), "moved")
    assert confirmations == 0

    monkeypatch.setattr(validator, "_complete", original_complete)
    validator.reconcile_ready_promotions()

    assert repository.get(created.intent_id, "generation-a").status == "completed"


def test_worker_recovers_validation_rename_before_ready_commit(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, _now)
    queue = TaskQueue(database, _now)
    body = _valid_pdf()
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    incoming = tmp_path / "imports" / ".incoming"
    verify = incoming / f"{created.intent_id}.verify.pdf"
    with database.transaction() as connection:
        connection.execute(
            "UPDATE import_intents SET status='validating' WHERE id=?",
            (str(created.intent_id),),
        )
    trusted = _trusted(tmp_path)
    held = trusted.open_incoming(created.intent_id, "uploaded.pdf")
    held.rename_incoming(created.intent_id, "verify.pdf")
    held.close()

    ImportValidator(database, queue, _trusted(tmp_path)).validate_and_promote(created.intent_id)

    assert repository.get(created.intent_id, "generation-a").status == "completed"
    assert not verify.exists()


@pytest.mark.parametrize("cleanup_allowed", [True, False])
def test_hashed_verify_cleanup_obeys_journal_state(
    tmp_path: Path, database, cleanup_allowed: bool
) -> None:
    body = (
        b"%PDF-1.7\ninvalid-after-hash\n%%EOF"
        if cleanup_allowed
        else _valid_pdf()
    )
    repository = ImportIntentRepository(database, _now)
    queue = TaskQueue(database, _now)
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    trusted = _trusted(tmp_path)
    held = trusted.open_incoming(created.intent_id, "uploaded.pdf")
    held.rename_incoming(created.intent_id, "verify.pdf")
    held.close()
    final = trusted.library_directory / "2026" / "07" / ("0" * 64 + ".pdf")
    with database.transaction() as connection:
        if cleanup_allowed:
            connection.execute(
                "UPDATE import_intents SET status='validating' WHERE id=?",
                (str(created.intent_id),),
            )
        else:
            connection.execute(
                """
                UPDATE import_intents SET status='ready_to_promote',content_sha256=?,
                  final_path_key=? WHERE id=?
                """,
                ("0" * 64, str(final), str(created.intent_id)),
            )

    ImportValidator(database, queue, trusted).validate_and_promote(created.intent_id)

    verify = trusted.incoming_path(created.intent_id, "verify.pdf")
    if cleanup_allowed:
        assert not verify.exists()
        assert repository.get(created.intent_id, "generation-a").status == "failed"
    else:
        assert verify.read_bytes() == body
        assert repository.get(created.intent_id, "generation-a").status == "ready_to_promote"
    trusted.close()


def test_same_content_imports_share_one_promotion_and_each_get_confirmation(
    tmp_path: Path, database
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    queue = TaskQueue(database, now)
    body = _valid_pdf()
    intents = [
        repository.create("generation-a", f"paper-{index}.pdf", len(body), "application/pdf")
        for index in range(2)
    ]
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    for intent in intents:
        asyncio.run(service.consume(intent.intent_id, intent.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, _trusted(tmp_path))

    for intent in intents:
        validator.validate_and_promote(intent.intent_id)

    with database.connect() as connection:
        promotions = connection.execute("SELECT COUNT(*) FROM import_promotions").fetchone()[0]
        confirmations = connection.execute(
            "SELECT intent_id,document_id FROM confirmed_imports ORDER BY intent_id"
        ).fetchall()
        documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert promotions == 1
    assert {row["intent_id"] for row in confirmations} == {str(item.intent_id) for item in intents}
    assert len({row["document_id"] for row in confirmations}) == 1
    assert documents == 1


def test_existing_document_must_match_full_confirmed_import_binding(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, _now)
    queue = TaskQueue(database, _now)
    body = _valid_pdf()
    first = repository.create("generation-a", "first.pdf", len(body), "application/pdf")
    second = repository.create("generation-a", "second.pdf", len(body), "application/pdf")
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    validator = ImportValidator(database, queue, _trusted(tmp_path))
    asyncio.run(service.consume(first.intent_id, first.upload_token, _chunks(body)))
    validator.validate_and_promote(first.intent_id)
    with database.transaction() as connection:
        connection.execute("UPDATE documents SET authorization_source='user'")
    asyncio.run(service.consume(second.intent_id, second.upload_token, _chunks(body)))

    validator.validate_and_promote(second.intent_id)

    view = repository.get(second.intent_id, "generation-a")
    assert (view.status, view.error_code) == ("failed", "document_binding_conflict")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 1


def test_true_concurrent_same_hash_imports_complete_once_each(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, _now)
    queue = TaskQueue(database, _now)
    body = _valid_pdf()
    intents = [
        repository.create("generation-a", f"parallel-{index}.pdf", len(body), "application/pdf")
        for index in range(2)
    ]
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    for intent in intents:
        asyncio.run(service.consume(intent.intent_id, intent.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, _trusted(tmp_path))
    barrier = Barrier(3)

    def validate(intent_id) -> None:
        barrier.wait()
        validator.validate_and_promote(intent_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(validate, intent.intent_id) for intent in intents]
        barrier.wait()
        for future in futures:
            future.result(timeout=10)

    assert [repository.get(item.intent_id, "generation-a").status for item in intents] == [
        "completed",
        "completed",
    ]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM import_promotions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 2


def test_owner_crash_wakes_waiter_and_reuses_persisted_path_across_month(
    tmp_path: Path, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import literature_workbench.features.library.import_service as service_module

    class ClockDatetime(datetime):
        current = datetime(2026, 7, 31, 23, 59, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            value = cls.current
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(service_module, "datetime", ClockDatetime)
    repository = ImportIntentRepository(database, lambda: ClockDatetime.current)
    queue = TaskQueue(database, lambda: ClockDatetime.current)
    body = _valid_pdf()
    owner = repository.create("generation-a", "owner.pdf", len(body), "application/pdf")
    waiter = repository.create("generation-a", "waiter.pdf", len(body), "application/pdf")
    upload = ImportService(database, repository, _trusted(tmp_path), "generation-a", queue=queue)
    for intent in (owner, waiter):
        asyncio.run(upload.consume(intent.intent_id, intent.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, _trusted(tmp_path))
    original_complete = validator._complete
    monkeypatch.setattr(
        validator,
        "_complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("owner_crash")),
    )
    with pytest.raises(RuntimeError, match="owner_crash"):
        validator.validate_and_promote(owner.intent_id)
    with database.connect() as connection:
        owner_path = str(
            connection.execute(
                "SELECT final_path_key FROM import_intents WHERE id=?", (str(owner.intent_id),)
            ).fetchone()[0]
        )
    assert "\\2026\\07\\" in owner_path

    ClockDatetime.current = datetime(2026, 8, 1, 0, 1, tzinfo=UTC)
    with pytest.raises(ImportPromotionPending, match="promotion_owner_pending"):
        validator.validate_and_promote(waiter.intent_id)
    with database.connect() as connection:
        waiter_path = str(
            connection.execute(
                "SELECT final_path_key FROM import_intents WHERE id=?", (str(waiter.intent_id),)
            ).fetchone()[0]
        )
    assert waiter_path == owner_path

    monkeypatch.setattr(validator, "_complete", original_complete)
    validator.validate_and_promote(owner.intent_id)
    validator.validate_and_promote(waiter.intent_id)

    assert repository.get(owner.intent_id, "generation-a").status == "completed"
    assert repository.get(waiter.intent_id, "generation-a").status == "completed"


def test_exact_cleanup_never_deletes_unrelated_incoming_file(tmp_path: Path, database) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    created = repository.create("generation-a", "paper.pdf", 99, "application/pdf")
    service = ImportService(database, repository, _trusted(tmp_path), "generation-a")
    sibling = tmp_path / "imports" / ".incoming" / "unrelated.part"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_bytes(b"keep")

    with pytest.raises(ValueError, match="upload_size_mismatch"):
        asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(b"%PDF-1.7")))

    assert sibling.read_bytes() == b"keep"


def test_manually_placed_managed_pdf_is_never_discovered_or_authorized(
    tmp_path: Path, database
) -> None:
    def now() -> datetime:
        return datetime.now(UTC)

    queue = TaskQueue(database, now)
    inspector = SafeWindowsFileInspector()
    validator = ImportValidator(database, queue, _trusted(tmp_path), inspector=inspector)
    managed = validator.ensure_managed_import_library()
    manual = managed.canonical_path / "manual.pdf"
    manual.write_bytes(_valid_pdf())
    scanner = LibraryScanner(
        database,
        LibraryRepository(database, queue),
        inspector,
        queue,
    )

    summary = scanner.scan_library(managed.id)

    assert summary.pdf_paths == 0
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0
        policy = connection.execute(
            "SELECT discovery_mode,cloud_pdf_auto_allowed,authorization_source,status FROM libraries"
        ).fetchone()
    assert tuple(policy) == ("managed_import", 1, "managed_import_internal", "active")


def test_trusted_handle_blocks_content_swap_and_preserves_identity_through_rename(
    tmp_path: Path,
) -> None:
    trusted = TrustedImportRoot.create(tmp_path / "data")
    body = _valid_pdf()
    intent_id = __import__("uuid").uuid4()
    handle = trusted.create_incoming(intent_id)
    handle.write(body)
    handle.flush()
    part = trusted.incoming_path(intent_id, "part")
    attacker = tmp_path / "attacker.pdf"
    attacker.write_bytes(_valid_pdf())

    with pytest.raises(PermissionError):
        __import__("os").replace(attacker, part)
    initial = handle.metadata()
    assert handle.sha256() == __import__("hashlib").sha256(body).hexdigest()
    with pytest.raises(PermissionError):
        __import__("os").replace(attacker, part)
    assert handle.probe_pdf(timeout_seconds=10) is True
    with pytest.raises(PermissionError):
        __import__("os").replace(attacker, part)
    handle.rename_incoming(intent_id, "uploaded.pdf")
    renamed = handle.metadata()
    with pytest.raises(PermissionError):
        __import__("os").replace(attacker, renamed.final_path)
    handle.rename_final("2026", "07", "held.pdf")
    final = handle.metadata()
    with pytest.raises(PermissionError):
        __import__("os").replace(attacker, final.final_path)

    assert (final.volume_id, final.file_id) == (initial.volume_id, initial.file_id)
    assert renamed.final_path == trusted.incoming_path(intent_id, "uploaded.pdf")
    assert final.final_path == trusted.library_directory / "2026" / "07" / "held.pdf"
    handle.close()
    trusted.close()


def test_trusted_handle_cleanup_is_exact_and_keeps_sibling(tmp_path: Path) -> None:
    trusted = TrustedImportRoot.create(tmp_path / "data")
    intent_id = __import__("uuid").uuid4()
    handle = trusted.create_incoming(intent_id)
    handle.write(b"%PDF-1.7")
    handle.flush()
    sibling = trusted.incoming_directory / "sibling.part"
    sibling.write_bytes(b"keep")

    handle.delete()
    handle.close()

    assert not trusted.incoming_path(intent_id, "part").exists()
    assert sibling.read_bytes() == b"keep"
    trusted.close()


def test_trusted_root_rejects_reparse_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "imports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="trusted_import_root_unsafe"):
        TrustedImportRoot.create(data)


@pytest.mark.parametrize("junction_level", ["year", "month"])
def test_final_directory_rejects_year_or_month_junction(
    tmp_path: Path, junction_level: str
) -> None:
    trusted = TrustedImportRoot.create(tmp_path / "data")
    outside = tmp_path / "outside" / junction_level
    outside.mkdir(parents=True)
    year = trusted.library_directory / "2026"
    if junction_level == "year":
        year.symlink_to(outside, target_is_directory=True)
    else:
        year.mkdir()
        (year / "07").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="trusted_import_root_unsafe"):
        trusted.final_directories("2026", "07")
    trusted.close()


def test_held_month_parent_blocks_check_after_replace(tmp_path: Path) -> None:
    trusted = TrustedImportRoot.create(tmp_path / "data")
    directories = trusted.final_directories("2026", "07")
    month = trusted.library_directory / "2026" / "07"
    displaced = tmp_path / "displaced-month"

    with pytest.raises(PermissionError):
        __import__("os").replace(month, displaced)

    for directory in reversed(directories):
        directory.close()
    trusted.close()


def test_probe_cancellation_terminates_only_its_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import literature_workbench.features.library.import_files as import_files

    trusted = TrustedImportRoot.create(tmp_path / "data")
    intent_id = __import__("uuid").uuid4()
    handle = trusted.create_incoming(intent_id)
    handle.write(_valid_pdf())
    handle.flush()
    started = Event()
    cancelled = Event()
    children = []
    real_popen = import_files.subprocess.Popen

    def sleeping_probe(*_args, **kwargs):
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **kwargs,
        )
        children.append(process)
        started.set()
        return process

    class Cancellation:
        def raise_if_cancelled(self) -> None:
            if cancelled.is_set():
                raise asyncio.CancelledError

    monkeypatch.setattr(import_files.subprocess, "Popen", sleeping_probe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            handle.probe_pdf,
            timeout_seconds=30,
            cancellation=Cancellation(),
        )
        assert started.wait(2)
        cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            future.result(timeout=2)

    assert len(children) == 1
    assert children[0].poll() is not None
    handle.close()
    trusted.close()
