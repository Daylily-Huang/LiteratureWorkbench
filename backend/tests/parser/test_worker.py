from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from conftest import ParserLibraryFixture
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.parser.mineru import ProcessOutcome
from literature_workbench.features.parser.normalizer import ParseRunRepository
from literature_workbench.features.parser.worker import ParseWorkerService
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
