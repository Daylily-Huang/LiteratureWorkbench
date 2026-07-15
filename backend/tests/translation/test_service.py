from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.translation.repository import (
    SelectionRejected,
    TranslationRepository,
)
from literature_workbench.features.translation.service import (
    TranslationRequest,
    TranslationService,
)
from literature_workbench.worker import Worker, translation_job_handlers


@dataclass(slots=True)
class FakeDeepSeek:
    result: str = "深度求索译文"
    error: str | None = None
    calls: list[object] = field(default_factory=list)

    def translate_selection(self, proof: object) -> str:
        self.calls.append(proof)
        if self.error is not None:
            raise EgressRejected(self.error)
        return self.result


@dataclass(slots=True)
class FakeQwenProcess:
    calls: int = 0

    def ensure_running(self) -> object:
        self.calls += 1
        return object()


@dataclass(slots=True)
class FakeQwen:
    result: str = "本地译文"
    calls: list[str] = field(default_factory=list)

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.result


@pytest.fixture
def translation_db(tmp_path) -> tuple[Database, dict[str, str]]:
    database = Database.open(tmp_path / "translation.sqlite3")
    run_migrations(database)
    now = datetime.now(UTC).isoformat()
    ids = {
        "library": str(uuid4()),
        "document": str(uuid4()),
        "version": str(uuid4()),
        "content": "a" * 64,
        "run": str(uuid4()),
    }
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO libraries VALUES(?,?,?,?,?,1,'monitored',1,
                   'test','active',?,?)""",
            (ids["library"], "/library", "/library", "vol", "root", now, now),
        )
        connection.execute(
            """INSERT INTO contents(
                 sha256,size_bytes,mime_type,page_count,current_parse_run_id,status,
                 created_at,updated_at) VALUES(?,12,'application/pdf',1,NULL,'completed',?,?)""",
            (ids["content"], now, now),
        )
        connection.execute(
            """INSERT INTO documents(
                 id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                 mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                 auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,'paper.pdf','pdf',12,1,'vol','file',1,'parse',1,
                        'registered_library','valid_pdf',?,?)""",
            (ids["document"], ids["library"], ids["content"], "/library/paper.pdf",
             "/library/paper.pdf", now, now),
        )
        connection.execute(
            """INSERT INTO document_versions VALUES(?,?,?,12,1,'vol','file',1,?)""",
            (ids["version"], ids["document"], ids["content"], now),
        )
        connection.execute(
            """INSERT INTO parse_runs(
                 id,content_sha256,parser_name,parser_version,status,created_at,updated_at,
                 completed_at) VALUES(?,?,'pymupdf','1','completed',?,?,?)""",
            (ids["run"], ids["content"], now, now, now),
        )
        connection.execute(
            "UPDATE contents SET current_parse_run_id=? WHERE sha256=?",
            (ids["run"], ids["content"]),
        )
        connection.execute(
            """INSERT INTO pages VALUES(?,0,1,?,
                 'Alpha beta selected words gamma delta',
                 'Alpha  beta\nselected words  gamma delta',612,792,'[]',1)""",
            (ids["content"], ids["run"]),
        )
    return database, ids


def request(ids: dict[str, str], *, text: str = "selected   words") -> TranslationRequest:
    return TranslationRequest(
        client_task_id=uuid4(),
        document_id=UUID(ids["document"]),
        document_version_id=UUID(ids["version"]),
        content_id=ids["content"],
        physical_page=1,
        selected_text=text,
        target_language="zh-CN",
    )


def service(translation_db, *, deepseek=None, qwen=None, process=None) -> TranslationService:
    database, _ = translation_db

    def clock() -> datetime:
        return datetime.now(UTC)

    return TranslationService(
        TranslationRepository(database, clock),
        SettingsRepository(database),
        TaskQueue(database, clock),
        deepseek or FakeDeepSeek(),
        qwen or FakeQwen(),
        process or FakeQwenProcess(),
    )


def test_rejects_forged_selection_and_stale_identity_before_any_model_call(
    translation_db,
) -> None:
    database, ids = translation_db
    deepseek, qwen, process = FakeDeepSeek(), FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    with pytest.raises(SelectionRejected, match="selection_not_current"):
        subject.submit("session", request(ids, text="forged text"))
    stale = request(ids)
    with database.transaction() as connection:
        connection.execute("UPDATE documents SET status='missing' WHERE id=?", (ids["document"],))
    with pytest.raises(SelectionRejected, match="selection_not_current"):
        subject.submit("session", stale)

    assert deepseek.calls == []
    assert qwen.calls == []
    assert process.calls == 0


def test_online_completion_is_cached_by_provider_model_and_selection(translation_db) -> None:
    _, ids = translation_db
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)

    first = subject.submit("session", request(ids))
    subject.process_task(first.id)
    second = subject.submit("session", request(ids))
    subject.process_task(second.id)

    assert len(deepseek.calls) == 1
    assert subject.get(first.id).status == "completed"
    cached = subject.get(second.id)
    assert cached.status == "completed"
    assert cached.translated_text == "深度求索译文"
    assert cached.provider == "deepseek"


@pytest.mark.parametrize(
    "code",
    [
        "deepseek_key_unavailable",
        "credential_store_failed",
        "deepseek_authorization_failed",
        "deepseek_request_rejected",
    ],
)
def test_credential_or_configuration_failure_is_terminal_without_fallback(
    translation_db, code: str
) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(qwen_fallback_enabled=True)
    deepseek, qwen, process = FakeDeepSeek(error=code), FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    handled = asyncio.run(
        Worker(
            queue,
            "translation-worker",
            translation_job_handlers(subject),
        ).run_once()
    )

    assert handled is True
    failed = subject.get(task.id)
    assert failed.status == "failed"
    assert failed.error_code == code
    assert qwen.calls == []
    assert process.calls == 0


def test_known_transient_deepseek_failure_falls_back_to_qwen_once(translation_db) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(qwen_fallback_enabled=True)
    deepseek = FakeDeepSeek(error="deepseek_total_timeout")
    qwen, process = FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    subject.process_task(task.id)

    completed = subject.get(task.id)
    assert completed.status == "completed"
    assert completed.provider == "qwen"
    assert completed.model == "literature-workbench-qwen"
    assert completed.fallback_reason == "deepseek_total_timeout"
    assert len(deepseek.calls) == len(qwen.calls) == process.calls == 1


def test_local_only_routes_directly_to_qwen(translation_db) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(default_translation_mode="local_only")
    deepseek, qwen, process = FakeDeepSeek(), FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    subject.process_task(task.id)

    assert subject.get(task.id).provider == "qwen"
    assert deepseek.calls == []
    assert len(qwen.calls) == process.calls == 1


def test_recovered_running_task_never_repeats_an_ambiguous_deepseek_send(
    translation_db,
) -> None:
    database, ids = translation_db
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    task = subject.submit("session", request(ids))
    with database.transaction() as connection:
        connection.execute(
            "UPDATE translation_tasks SET status='running' WHERE id=?", (str(task.id),)
        )

    recovered = subject.process_task(task.id)

    assert recovered.status == "failed"
    assert recovered.error_code == "translation_send_ambiguous"
    assert deepseek.calls == []
