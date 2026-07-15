from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from conftest import ParserLibraryFixture
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.parser.mineru import ProcessOutcome
from literature_workbench.features.parser.models import ParsedPage
from literature_workbench.features.parser.normalizer import ParseRunRepository
from literature_workbench.features.parser.worker import ParseWorkerService
from literature_workbench.features.parser.workspace import TrustedParserWorkspace
from literature_workbench.worker import Worker, parser_job_handlers


def _pdf_bytes(pages: int = 2, text: bool = True) -> bytes:
    import fitz

    document = fitz.open()
    try:
        for _ in range(pages):
            page = document.new_page(width=400, height=600)
            if text:
                page.insert_textbox(
                    fitz.Rect(20, 20, 380, 580),
                    "direct extraction academic page text " * 5,
                    fontsize=10,
                )
        return document.tobytes()
    finally:
        document.close()


def _queue(parser_library: ParserLibraryFixture) -> TaskQueue:
    from datetime import UTC, datetime

    return TaskQueue(parser_library.database, lambda: datetime.now(UTC))


def test_worker_direct_pipeline_uses_staging_and_switches_complete_pointer(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("source-direct.pdf", _pdf_bytes())
    before = hashlib.sha256(registered.path.read_bytes()).hexdigest()
    queue = _queue(parser_library)
    queue.enqueue(
        "parse_pdf",
        f"parse:{registered.content_id}",
        {"content_id": str(registered.content_id)},
    )
    service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
    )
    worker = Worker(queue, "parser-worker", parser_job_handlers(service))

    assert asyncio.run(worker.run_once()) is True

    repository = ParseRunRepository(parser_library.database)
    run_id = repository.current_run_id(registered.content_id)
    assert run_id is not None
    page = repository.read_current_page(registered.content_id, 1)
    assert page is not None
    assert page.parser_name == "pymupdf"
    assert "direct extraction" in page.normalized_text
    assert hashlib.sha256(registered.path.read_bytes()).hexdigest() == before
    assert list((parser_library.data_root / "staging" / registered.content_id).glob("*")) == []


def test_forced_mineru_uses_only_staging_and_creates_trailing_blank_page(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("original-library.pdf", _pdf_bytes(text=False))
    observed_inputs: list[Path] = []

    class FakeMinerU:
        def run(self, staged: object, output: Path, language: str) -> ProcessOutcome:
            staged_path = staged.path  # type: ignore[attr-defined]
            observed_inputs.append(staged_path)
            assert "staging" in staged_path.parts
            assert staged_path != registered.path
            assert language == "ch"
            result = output / staged_path.stem / "auto"
            result.mkdir(parents=True)
            (result / f"{staged_path.stem}_content_list.json").write_text(
                json.dumps(
                    [{"page_idx": 0, "type": "text", "text": "OCR 第一页", "bbox": [0, 0, 500, 100]}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return ProcessOutcome(0, b"", b"")

    queue = _queue(parser_library)
    queue.enqueue(
        "parse_pdf",
        f"manual:{registered.content_id}",
        {
            "document_id": str(registered.document_id),
            "content_id": str(registered.content_id),
            "strategy": "mineru",
            "ocr_language": "ch",
        },
    )
    service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: FakeMinerU(),  # type: ignore[arg-type]
    )
    worker = Worker(queue, "parser-worker", parser_job_handlers(service))

    assert asyncio.run(worker.run_once()) is True

    repository = ParseRunRepository(parser_library.database)
    assert repository.read_current_page(registered.content_id, 1).normalized_text == "OCR 第一页"  # type: ignore[union-attr]
    assert repository.read_current_page(registered.content_id, 2).normalized_text == ""  # type: ignore[union-attr]
    assert len(observed_inputs) == 1
    with parser_library.database.connect() as connection:
        job = connection.execute("SELECT checkpoint_json FROM jobs").fetchone()
    assert job is not None
    checkpoint = json.loads(job["checkpoint_json"])
    assert str(registered.path) not in json.dumps(checkpoint)


def test_failed_mineru_attempt_preserves_previous_pointer_and_reports_fixed_error(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("retry.pdf", _pdf_bytes())
    queue = _queue(parser_library)
    first_id = queue.enqueue(
        "parse_pdf", f"first:{registered.content_id}", {"content_id": str(registered.content_id)}
    )
    direct_service = ParseWorkerService(
        parser_library.database, parser_library.inspector, parser_library.data_root
    )
    assert asyncio.run(
        Worker(queue, "direct", parser_job_handlers(direct_service)).run_once()
    ) is True
    old_pointer = ParseRunRepository(parser_library.database).current_run_id(
        registered.content_id
    )
    assert old_pointer is not None

    class MissingOutputMinerU:
        def run(self, staged: object, output: Path, language: str) -> ProcessOutcome:
            output.mkdir(parents=True, exist_ok=True)
            return ProcessOutcome(0, b"SENTINEL document text", b"SENTINEL document text")

    failed_id = queue.enqueue(
        "parse_pdf",
        f"failed:{registered.content_id}",
        {
            "document_id": str(registered.document_id),
            "content_id": str(registered.content_id),
            "strategy": "mineru",
            "ocr_language": "en",
        },
    )
    failed_service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: MissingOutputMinerU(),  # type: ignore[arg-type]
    )
    assert asyncio.run(
        Worker(queue, "failed", parser_job_handlers(failed_service)).run_once()
    ) is True

    repository = ParseRunRepository(parser_library.database)
    assert repository.current_run_id(registered.content_id) == old_pointer
    with parser_library.database.connect() as connection:
        failed_job = connection.execute(
            "SELECT status,error_code,checkpoint_json FROM jobs WHERE id=?", (failed_id,)
        ).fetchone()
        failed_run = connection.execute(
            "SELECT status,error_code FROM parse_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert failed_job is not None
    assert tuple(failed_job)[:2] == ("failed", "mineru_output_missing")
    assert "SENTINEL" not in str(tuple(failed_job))
    assert failed_run is not None
    assert tuple(failed_run) == ("failed", "mineru_output_missing")
    assert first_id != failed_id


def test_read_only_suppresses_automatic_parse_without_staging(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("read-only.pdf", _pdf_bytes())
    with parser_library.database.transaction() as connection:
        connection.execute(
            "UPDATE documents SET parse_preference='read_only' WHERE id=?",
            (str(registered.document_id),),
        )
    queue = _queue(parser_library)
    queue.enqueue(
        "parse_pdf", f"auto:{registered.content_id}", {"content_id": str(registered.content_id)}
    )
    service = ParseWorkerService(
        parser_library.database, parser_library.inspector, parser_library.data_root
    )

    assert asyncio.run(
        Worker(queue, "read-only", parser_job_handlers(service)).run_once()
    ) is True

    assert ParseRunRepository(parser_library.database).current_run_id(
        registered.content_id
    ) is None
    assert not (parser_library.data_root / "staging").exists()


def test_same_content_cloud_eligibility_revalidates_each_authorized_single_link_source(
    parser_library: ParserLibraryFixture,
) -> None:
    payload = _pdf_bytes()
    first = parser_library.register("a-copy.pdf", payload)
    second = parser_library.register("b-copy.pdf", payload)
    assert first.content_id == second.content_id
    queue = _queue(parser_library)
    queue.enqueue(
        "parse_pdf", f"cloud:{first.content_id}", {"content_id": str(first.content_id)}
    )
    service = ParseWorkerService(
        parser_library.database, parser_library.inspector, parser_library.data_root
    )

    assert asyncio.run(
        Worker(queue, "cloud-worker", parser_job_handlers(service)).run_once()
    ) is True

    with parser_library.database.connect() as connection:
        rows = connection.execute(
            "SELECT auto_cloud_eligible FROM documents WHERE content_sha256=? ORDER BY path_key",
            (first.content_id,),
        ).fetchall()
    assert [int(row["auto_cloud_eligible"]) for row in rows] == [1, 1]


def test_auto_ocr_language_uses_bounded_extracted_body_not_only_metadata(
    parser_library: ParserLibraryFixture,
) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((36, 72), "这是中文正文用于选择识别语言", fontname="china-s")
    payload = document.tobytes()
    document.close()
    registered = parser_library.register("han-body.pdf", payload)
    observed_languages: list[str] = []

    class FakeMinerU:
        def run(self, staged: object, output: Path, language: str) -> ProcessOutcome:
            observed_languages.append(language)
            staged_path = staged.path  # type: ignore[attr-defined]
            result = output / staged_path.stem / "auto"
            result.mkdir(parents=True)
            (result / f"{staged_path.stem}_content_list.json").write_text(
                json.dumps([], ensure_ascii=False), encoding="utf-8"
            )
            return ProcessOutcome(0, b"", b"")

    queue = _queue(parser_library)
    queue.enqueue(
        "parse_pdf",
        f"language:{registered.content_id}",
        {"content_id": str(registered.content_id), "strategy": "auto"},
    )
    service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: FakeMinerU(),  # type: ignore[arg-type]
    )

    assert asyncio.run(
        Worker(queue, "language-worker", parser_job_handlers(service)).run_once()
    ) is True
    assert observed_languages == ["ch"]


def test_real_mineru_work_directory_swap_is_blocked_during_subprocess(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("mineru-work-race.pdf", _pdf_bytes(text=False))

    class RaceMinerU:
        def run(self, staged: object, output: Path, language: str) -> ProcessOutcome:
            output.mkdir(parents=True, exist_ok=True)
            moved = output.with_name(f"{output.name}-moved")
            with pytest.raises(PermissionError):
                os.replace(output, moved)
            staged_path = staged.path  # type: ignore[attr-defined]
            result = output / staged_path.stem / "auto"
            result.mkdir(parents=True)
            (result / f"{staged_path.stem}_content_list.json").write_text(
                "[]", encoding="utf-8"
            )
            return ProcessOutcome(0, b"", b"")

    queue = _queue(parser_library)
    job_id = queue.enqueue(
        "parse_pdf",
        f"work-race:{registered.content_id}",
        {
            "document_id": str(registered.document_id),
            "content_id": str(registered.content_id),
            "strategy": "mineru",
        },
    )
    service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: RaceMinerU(),  # type: ignore[arg-type]
    )

    assert asyncio.run(
        Worker(queue, "work-race", parser_job_handlers(service)).run_once()
    ) is True
    with parser_library.database.connect() as connection:
        job = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id=?", (str(job_id),)
        ).fetchone()
    assert job is not None and tuple(job) == ("completed", None)


def test_retry_rebuilds_pointer_after_crash_between_db_complete_and_publish(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("publish-crash.pdf", _pdf_bytes(pages=1))
    with parser_library.database.transaction() as connection:
        connection.execute(
            "UPDATE contents SET page_count=1,status='valid_pdf' WHERE sha256=?",
            (registered.content_id,),
        )
    queue = _queue(parser_library)
    job_id = queue.enqueue(
        "parse_pdf",
        f"crash:{registered.content_id}",
        {
            "document_id": str(registered.document_id),
            "content_id": str(registered.content_id),
            "strategy": "mineru",
        },
    )
    repository = ParseRunRepository(parser_library.database)
    run_id = repository.create_run(
        registered.content_id,
        "mineru",
        "mineru-test",
        source_job_id=job_id,
    )
    output_relative = f"parse-results/{registered.content_id}/{run_id}"
    repository.set_output_dir(run_id, output_relative)
    repository.complete_run(
        run_id,
        [
            ParsedPage(
                content_id=registered.content_id,
                page_index=0,
                physical_page=1,
                parse_run_id=run_id,
                parser_name="mineru",
                parser_version="mineru-test",
                normalized_text="recovered",
                raw_text="recovered",
                width_points=400,
                height_points=600,
                coordinate_reliable=True,
                blocks=[],
            )
        ],
    )
    workspace = TrustedParserWorkspace.create(parser_library.data_root)
    work = workspace.create_mineru_work(run_id)
    (work.path / "validated.marker").write_text("ok", encoding="utf-8")
    result = workspace.promote_mineru_work(registered.content_id, run_id, work)
    result.close()
    workspace.close()
    pointer = (
        parser_library.data_root
        / "parse-results"
        / registered.content_id
        / "current.json"
    )
    assert not pointer.exists()

    class MustNotRerunMinerU:
        def run(self, *_args: object) -> ProcessOutcome:
            raise AssertionError("completed immutable result must be recovered")

    service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: MustNotRerunMinerU(),  # type: ignore[arg-type]
    )
    assert asyncio.run(
        Worker(queue, "crash-recovery", parser_job_handlers(service)).run_once()
    ) is True

    assert json.loads(pointer.read_text(encoding="utf-8"))["attempt_id"] == str(run_id)
    with parser_library.database.connect() as connection:
        job = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id=?", (str(job_id),)
        ).fetchone()
    assert job is not None and tuple(job) == ("completed", None)


def test_stale_completed_job_cannot_publish_over_newer_current_result(
    parser_library: ParserLibraryFixture,
) -> None:
    registered = parser_library.register("stale-publish.pdf", _pdf_bytes(pages=1))
    with parser_library.database.transaction() as connection:
        connection.execute(
            "UPDATE contents SET page_count=1,status='valid_pdf' WHERE sha256=?",
            (registered.content_id,),
        )
    queue = _queue(parser_library)
    old_job = queue.enqueue(
        "parse_pdf",
        f"old:{registered.content_id}",
        {"content_id": str(registered.content_id), "strategy": "mineru"},
    )
    new_job = queue.enqueue(
        "parse_pdf",
        f"new:{registered.content_id}",
        {"content_id": str(registered.content_id), "strategy": "mineru"},
    )
    repository = ParseRunRepository(parser_library.database)

    def complete(job_id: object, text: str) -> tuple[object, object]:
        run_id = repository.create_run(
            registered.content_id,
            "mineru",
            "mineru-test",
            source_job_id=job_id,  # type: ignore[arg-type]
        )
        repository.set_output_dir(
            run_id, f"parse-results/{registered.content_id}/{run_id}"
        )
        repository.complete_run(
            run_id,
            [
                ParsedPage(
                    content_id=registered.content_id,
                    page_index=0,
                    physical_page=1,
                    parse_run_id=run_id,
                    parser_name="mineru",
                    parser_version="mineru-test",
                    normalized_text=text,
                    raw_text=text,
                    width_points=400,
                    height_points=600,
                    coordinate_reliable=True,
                    blocks=[],
                )
            ],
        )
        workspace = TrustedParserWorkspace.create(parser_library.data_root)
        work = workspace.create_mineru_work(run_id)
        (work.path / "validated.marker").write_text(text, encoding="utf-8")
        result = workspace.promote_mineru_work(registered.content_id, run_id, work)
        workspace.close()
        return run_id, result

    old_run, old_result = complete(old_job, "old")
    old_result.close()  # type: ignore[attr-defined]
    new_run, new_result = complete(new_job, "new")
    new_result.close()  # type: ignore[attr-defined]
    new_workspace = TrustedParserWorkspace.create(parser_library.data_root)
    reopened = new_workspace.open_mineru_result(registered.content_id, new_run)  # type: ignore[arg-type]
    new_workspace.publish_result_pointer(registered.content_id, new_run, reopened)  # type: ignore[arg-type]
    reopened.close()
    new_workspace.close()
    pointer = (
        parser_library.data_root
        / "parse-results"
        / registered.content_id
        / "current.json"
    )

    service = ParseWorkerService(
        parser_library.database, parser_library.inspector, parser_library.data_root
    )
    assert asyncio.run(
        Worker(queue, "stale-publish", parser_job_handlers(service)).run_once()
    ) is True

    assert json.loads(pointer.read_text(encoding="utf-8"))["attempt_id"] == str(new_run)
    assert repository.current_run_id(registered.content_id) == new_run
    with parser_library.database.connect() as connection:
        job = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id=?", (str(old_job),)
        ).fetchone()
    assert job is not None and tuple(job) == ("failed", "stale_parse_publication")
    assert old_run != new_run


def test_older_normal_mineru_path_cannot_publish_after_newer_run_wins(
    parser_library: ParserLibraryFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = parser_library.register("publish-race.pdf", _pdf_bytes(text=False))
    queue = _queue(parser_library)

    class FakeMinerU:
        def __init__(self, text: str) -> None:
            self._text = text

        def run(self, staged: object, output: Path, language: str) -> ProcessOutcome:
            del language
            staged_path = staged.path  # type: ignore[attr-defined]
            result = output / staged_path.stem / "auto"
            result.mkdir(parents=True)
            (result / f"{staged_path.stem}_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "page_idx": 0,
                            "type": "text",
                            "text": self._text,
                            "bbox": [0, 0, 100, 100],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            return ProcessOutcome(0, b"", b"")

    old_job = queue.enqueue(
        "parse_pdf",
        f"publish-race-old:{registered.content_id}",
        {
            "document_id": str(registered.document_id),
            "content_id": str(registered.content_id),
            "strategy": "mineru",
        },
    )
    old_completed = threading.Event()
    let_old_publish = threading.Event()
    complete_calls = 0
    complete_lock = threading.Lock()
    original_complete = ParseRunRepository.complete_run

    def interleaved_complete(self: object, *args: object, **kwargs: object) -> object:
        nonlocal complete_calls
        result = original_complete(self, *args, **kwargs)  # type: ignore[arg-type]
        with complete_lock:
            complete_calls += 1
            call_number = complete_calls
        if call_number == 1:
            old_completed.set()
            assert let_old_publish.wait(10)
        return result

    monkeypatch.setattr(ParseRunRepository, "complete_run", interleaved_complete)
    old_service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: FakeMinerU("old"),  # type: ignore[arg-type]
    )
    old_result: list[bool] = []
    old_thread = threading.Thread(
        target=lambda: old_result.append(
            asyncio.run(
                Worker(queue, "publish-old", parser_job_handlers(old_service)).run_once()
            )
        )
    )
    old_thread.start()
    assert old_completed.wait(10)

    new_job = queue.enqueue(
        "parse_pdf",
        f"publish-race-new:{registered.content_id}",
        {
            "document_id": str(registered.document_id),
            "content_id": str(registered.content_id),
            "strategy": "mineru",
        },
    )
    new_service = ParseWorkerService(
        parser_library.database,
        parser_library.inspector,
        parser_library.data_root,
        mineru_factory=lambda _record: FakeMinerU("new"),  # type: ignore[arg-type]
    )
    assert asyncio.run(
        Worker(queue, "publish-new", parser_job_handlers(new_service)).run_once()
    ) is True
    let_old_publish.set()
    old_thread.join(10)
    assert not old_thread.is_alive()
    assert old_result == [True]

    repository = ParseRunRepository(parser_library.database)
    current = repository.current_run_id(registered.content_id)
    assert current is not None
    assert repository.read_current_page(registered.content_id, 1).normalized_text == "new"  # type: ignore[union-attr]
    pointer = json.loads(
        (
            parser_library.data_root
            / "parse-results"
            / registered.content_id
            / "current.json"
        ).read_text(encoding="utf-8")
    )
    assert pointer["attempt_id"] == str(current)
    with parser_library.database.connect() as connection:
        old = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id=?", (old_job,)
        ).fetchone()
        new = connection.execute(
            "SELECT status,error_code FROM jobs WHERE id=?", (new_job,)
        ).fetchone()
    assert old is not None and (old["status"], old["error_code"]) == (
        "failed",
        "stale_parse_publication",
    )
    assert new is not None and (new["status"], new["error_code"]) == (
        "completed",
        None,
    )
