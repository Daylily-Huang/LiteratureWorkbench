from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import fitz
import pytest

from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.library.import_intents import ImportIntentRepository
from literature_workbench.features.library.import_service import ImportService, ImportValidator
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector


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
        tmp_path,
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
    service = ImportService(database, repository, tmp_path, "generation-a", now=now)

    with pytest.raises(ValueError, match="upload_size_mismatch"):
        asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(b"%PDF-1.7")))

    assert not (tmp_path / "imports" / ".incoming" / f"{created.intent_id}.part").exists()


def test_worker_rejects_malformed_pdf_and_cleans_journaled_file(tmp_path: Path, database) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = b"%PDF-1.7\nnot structurally readable\n%%EOF"
    created = repository.create("generation-a", "bad.pdf", len(body), "application/pdf")
    service = ImportService(database, repository, tmp_path, "generation-a", now=now)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, TaskQueue(database, now), tmp_path)

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
    service = ImportService(database, repository, tmp_path, "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(repaired)))

    ImportValidator(database, queue, tmp_path).validate_and_promote(created.intent_id)

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
    service = ImportService(database, repository, tmp_path, "generation-a", queue=queue, now=now)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, tmp_path)

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
    tmp_path: Path, database, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = _valid_pdf()
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    queue = TaskQueue(database, now)
    service = ImportService(database, repository, tmp_path, "generation-a", queue=queue, now=now)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, tmp_path)
    digest = __import__("hashlib").sha256(body).hexdigest()
    final = tmp_path / "imports" / "library" / "2026" / "07" / f"{digest}.pdf"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"different")
    monkeypatch.setattr(validator, "_month_parts", lambda: ("2026", "07"))

    validator.validate_and_promote(created.intent_id)

    assert final.read_bytes() == b"different"
    assert repository.get(created.intent_id, "generation-a").status == "failed"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM confirmed_imports").fetchone()[0] == 0


def test_supervisor_recovers_full_receiving_part_but_worker_restart_does_not_touch_it(
    tmp_path: Path, database
) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    body = _valid_pdf()
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    repository.claim(created.intent_id, "generation-a", created.upload_token)
    part = tmp_path / "imports" / ".incoming" / f"{created.intent_id}.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(body)
    queue = TaskQueue(database, now)

    # Constructing/restarting only worker-side validation never reconciles receiving bytes.
    ImportValidator(database, queue, tmp_path)
    assert part.read_bytes() == body
    assert repository.get(created.intent_id, "generation-a").status == "receiving"

    ImportService(database, repository, tmp_path, "generation-a", queue=queue).reconcile_stale_receiving()

    assert not part.exists()
    assert (part.parent / f"{created.intent_id}.uploaded.pdf").exists()
    assert repository.get(created.intent_id, "generation-a").status == "uploaded"
    assert queue.count(kind="validate_import") == 1


def test_supervisor_cleans_only_exact_invalid_receiving_files(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, _now)
    created = repository.create("generation-a", "paper.pdf", 9, "application/pdf")
    repository.claim(created.intent_id, "generation-a", created.upload_token)
    incoming = tmp_path / "imports" / ".incoming"
    incoming.mkdir(parents=True)
    uploaded = incoming / f"{created.intent_id}.uploaded.pdf"
    part = incoming / f"{created.intent_id}.part"
    unrelated = incoming / "unrelated.part"
    uploaded.write_bytes(b"not-a-pdf")
    part.write_bytes(b"partial")
    unrelated.write_bytes(b"keep")

    ImportService(database, repository, tmp_path, "generation-a").reconcile_stale_receiving()

    assert repository.get(created.intent_id, "generation-a").status == "failed"
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
    service = ImportService(database, repository, tmp_path, "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, tmp_path)
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
    service = ImportService(database, repository, tmp_path, "generation-a", queue=queue)
    asyncio.run(service.consume(created.intent_id, created.upload_token, _chunks(body)))
    incoming = tmp_path / "imports" / ".incoming"
    uploaded = incoming / f"{created.intent_id}.uploaded.pdf"
    verify = incoming / f"{created.intent_id}.verify.pdf"
    with database.transaction() as connection:
        connection.execute(
            "UPDATE import_intents SET status='validating' WHERE id=?",
            (str(created.intent_id),),
        )
    ImportService._move_no_replace(uploaded, verify)

    ImportValidator(database, queue, tmp_path).validate_and_promote(created.intent_id)

    assert repository.get(created.intent_id, "generation-a").status == "completed"
    assert not verify.exists()


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
    service = ImportService(database, repository, tmp_path, "generation-a", queue=queue)
    for intent in intents:
        asyncio.run(service.consume(intent.intent_id, intent.upload_token, _chunks(body)))
    validator = ImportValidator(database, queue, tmp_path)

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


def test_exact_cleanup_never_deletes_unrelated_incoming_file(tmp_path: Path, database) -> None:
    now = _now
    repository = ImportIntentRepository(database, now)
    created = repository.create("generation-a", "paper.pdf", 99, "application/pdf")
    service = ImportService(database, repository, tmp_path, "generation-a")
    sibling = tmp_path / "imports" / ".incoming" / "unrelated.part"
    sibling.parent.mkdir(parents=True)
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
    validator = ImportValidator(database, queue, tmp_path, inspector=inspector)
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
