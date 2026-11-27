from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]
import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.parser.worker import ParseWorkerService
from literature_workbench.features.translation.repository import (
    SelectionRejected,
    TranslationRepository,
)
from literature_workbench.features.translation.service import (
    TranslationRequest,
    TranslationService,
)
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.worker import Worker, parser_job_handlers


@dataclass(slots=True)
class FakeDeepSeek:
    result: str = "DeepSeek 译文"
    error: str | None = None
    calls: list[object] = field(default_factory=list)

    def translate_selection(self, proof: object) -> str:
        self.calls.append(proof)
        if self.error is not None:
            raise EgressRejected(self.error)
        return self.result


@dataclass(slots=True)
class FakeQwen:
    calls: list[str] = field(default_factory=list)

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return "Qwen 备用译文"


@dataclass(slots=True)
class FakeQwenProcess:
    calls: int = 0

    def ensure_running(self) -> object:
        self.calls += 1
        return object()


def _clock() -> datetime:
    return datetime.now(UTC)


def _service(
    database: Database,
    deepseek: FakeDeepSeek,
    qwen: FakeQwen,
    process: FakeQwenProcess,
) -> TranslationService:
    return TranslationService(
        TranslationRepository(database, _clock),
        SettingsRepository(database),
        TaskQueue(database, _clock),
        deepseek,
        qwen,
        process,
    )


def _request(row: object, selected_text: str) -> TranslationRequest:
    values = row  # sqlite Row kept opaque outside this test helper
    return TranslationRequest(
        client_task_id=uuid4(),
        document_id=UUID(values["document_id"]),  # type: ignore[index]
        document_version_id=UUID(values["version_id"]),  # type: ignore[index]
        content_id=str(values["content_sha256"]),  # type: ignore[index]
        physical_page=1,
        selected_text=selected_text,
        target_language="zh-CN",
    )


def test_v01_isolated_library_translation_fallback_cache_and_restart(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    library_root = tmp_path / "library"
    library_root.mkdir()
    pdf_path = library_root / "paper.pdf"
    generated = fitz.open()
    page = generated.new_page()
    page.insert_text((72, 72), "Alpha selected words gamma delta")
    generated.save(pdf_path)
    generated.close()
    (library_root / "notes.txt").write_text("must never leave this machine", encoding="utf-8")
    database = Database.open(data_root / "db" / "literature-workbench.sqlite3")
    run_migrations(database)
    queue = TaskQueue(database, _clock)
    repository = LibraryRepository(database)
    inspector = SafeWindowsFileInspector()
    library = repository.register(
        inspector.inspect_root_directory(library_root),
        recursive=True,
        authorization_source="native_picker_confirmed",
    )
    scanner = LibraryScanner(database, repository, inspector, queue)

    summary = scanner.scan_library(library.id)

    assert summary.pdf_paths == 1
    assert summary.unsupported_paths == 1
    parser = ParseWorkerService(
        database,
        inspector,
        data_root,
        reconcile=scanner.reconcile_path,
    )
    with database.transaction() as connection:
        queued_parse = connection.execute(
            "SELECT id,payload_json FROM jobs WHERE kind='parse_pdf' AND status='queued'"
        ).fetchone()
        assert queued_parse is not None
        payload = json.loads(queued_parse["payload_json"])
        payload["strategy"] = "pymupdf"
        connection.execute(
            "UPDATE jobs SET payload_json=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), queued_parse["id"]),
        )
    assert asyncio.run(
        Worker(queue, "acceptance-parser", parser_job_handlers(parser)).run_once()
    )
    with database.transaction() as connection:
        row = connection.execute(
            """
            SELECT d.id AS document_id,d.content_sha256,v.id AS version_id
            FROM documents d JOIN document_versions v ON v.document_id=d.id
            WHERE d.display_name='paper.pdf'
            """
        ).fetchone()
        assert row is not None
        parsed = connection.execute(
            """
            SELECT p.raw_text,r.status,r.parser_name
            FROM contents c JOIN parse_runs r ON r.id=c.current_parse_run_id
            JOIN pages p ON p.parse_run_id=r.id AND p.content_sha256=c.sha256
            WHERE c.sha256=? AND p.physical_page=1
            """,
            (row["content_sha256"],),
        ).fetchone()
        assert parsed is not None
        assert parsed["status"] == "completed"
        assert parsed["parser_name"] == "pymupdf"
        assert "Alpha selected words gamma delta" in parsed["raw_text"]
        unsupported = connection.execute(
            "SELECT content_sha256,status FROM documents WHERE display_name='notes.txt'"
        ).fetchone()
        assert unsupported is not None
        assert unsupported["content_sha256"] is None
        assert unsupported["status"] == "unsupported_v01"

    deepseek = FakeDeepSeek()
    qwen = FakeQwen()
    qwen_process = FakeQwenProcess()
    service = _service(database, deepseek, qwen, qwen_process)
    first = service.submit("session-a", _request(row, "selected words"))
    assert service.process_task(first.id).translated_text == "DeepSeek 译文"

    cached = service.submit("session-a", _request(row, "selected words"))
    assert service.process_task(cached.id).translated_text == "DeepSeek 译文"
    assert len(deepseek.calls) == 1

    SettingsRepository(database).update(qwen_fallback_enabled=True)
    transient = FakeDeepSeek(error="deepseek_total_timeout")
    fallback = _service(database, transient, qwen, qwen_process)
    fallback_task = fallback.submit("session-a", _request(row, "gamma delta"))
    completed = fallback.process_task(fallback_task.id)
    assert completed.provider == "qwen"
    assert completed.fallback_reason == "deepseek_total_timeout"
    # One bounded retry precedes the fallback, so DeepSeek sees two attempts.
    assert len(transient.calls) == 2
    assert len(qwen.calls) == qwen_process.calls == 1

    database_path = database.path
    del service, fallback, database, queue, repository, scanner, parser
    reopened_database = Database.open(database_path)
    run_migrations(reopened_database)
    restarted_deepseek = FakeDeepSeek(result="must not be called")
    restarted = _service(
        reopened_database, restarted_deepseek, FakeQwen(), FakeQwenProcess()
    )
    after_restart = restarted.submit("session-b", _request(row, "selected words"))
    assert restarted.process_task(after_restart.id).translated_text == "DeepSeek 译文"
    assert restarted_deepseek.calls == []
    with reopened_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2

    with pytest.raises(SelectionRejected, match="selection_text_mismatch"):
        restarted.submit("session-b", _request(row, "must never leave this machine"))
    assert restarted_deepseek.calls == []
