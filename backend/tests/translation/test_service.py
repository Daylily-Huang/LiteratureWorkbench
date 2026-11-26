from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.translation.repository import (
    CachedTranslation,
    SelectionRejected,
    TranslationNotFound,
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
    error_sequence: list[str | None] | None = None
    calls: list[object] = field(default_factory=list)

    def translate_selection(self, proof: object) -> str:
        self.calls.append(proof)
        if self.error_sequence:
            outcome = self.error_sequence.pop(0)
            if outcome is not None:
                raise EgressRejected(outcome)
            return self.result
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


def test_startup_expires_translation_tasks_from_previous_session(translation_db) -> None:
    database, ids = translation_db
    def clock() -> datetime:
        return datetime.now(UTC)

    repository = TranslationRepository(database, clock)
    queue = TaskQueue(database, clock)
    subject = TranslationService(
        repository,
        SettingsRepository(database),
        queue,
        FakeDeepSeek(),
        FakeQwen(),
        FakeQwenProcess(),
    )
    old = subject.submit("old-session", request(ids))
    current = subject.submit("current-session", request(ids))

    assert repository.expire_other_sessions("current-session") == 1

    assert repository.get(old.id, session_generation="old-session").status == "failed"
    assert repository.get(current.id, session_generation="current-session").status == "queued"
    with database.connect() as connection:
        old_job = connection.execute(
            "SELECT status,error_code,retryable FROM jobs WHERE dedup_key=?",
            (f"translation:{old.id}",),
        ).fetchone()
    assert old_job is not None
    assert tuple(old_job) == ("failed", "translation_session_expired", 0)


def test_rejects_forged_selection_and_stale_identity_before_any_model_call(
    translation_db,
) -> None:
    database, ids = translation_db
    deepseek, qwen, process = FakeDeepSeek(), FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    with pytest.raises(SelectionRejected, match="selection_text_mismatch"):
        subject.submit("session", request(ids, text="forged text"))
    stale = request(ids)
    with database.transaction() as connection:
        connection.execute("UPDATE documents SET status='missing' WHERE id=?", (ids["document"],))
    with pytest.raises(SelectionRejected, match="selection_not_current"):
        subject.submit("session", stale)

    assert deepseek.calls == []
    assert qwen.calls == []
    assert process.calls == 0


def test_accepts_selection_from_current_parse_block_when_page_text_is_reordered(
    translation_db,
) -> None:
    database, ids = translation_db
    block_text = "REVIEWS Scaling-up camera traps: monitoring biodiversity"
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET blocks_json=? WHERE parse_run_id=?",
            (
                '[{"block_id":"0:1","kind":"title","text":'
                '"REVIEWS Scaling-up camera traps: monitoring biodiversity",'
                '"bbox_1000":[0,0,1000,100],"source":"pymupdf",'
                '"confidence":null}]',
                ids["run"],
            ),
        )

    task = service(translation_db).submit("session", request(ids, text=block_text))

    assert task.status == "queued"
    assert task.selected_text == block_text


def test_accepts_prose_selection_across_short_figure_labels(translation_db) -> None:
    database, ids = translation_db
    first = (
        "Dealing with errors requires enough typed loci to distinguish individuals "
        "and estimate the probability of identity."
    )
    continuation = (
        "More generally, parentage analysis should allow one or several genetic "
        "mismatches when the evidence supports it."
    )
    blocks = [
        {"block_id": "0:10", "kind": "text", "text": first},
        {"block_id": "0:11", "kind": "text", "text": "Father Mother"},
        {"block_id": "0:12", "kind": "text", "text": "149 153"},
        {"block_id": "0:13", "kind": "text", "text": "Offspring"},
        {"block_id": "0:16", "kind": "text", "text": continuation},
    ]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET blocks_json=? WHERE parse_run_id=?",
            (json.dumps(blocks), ids["run"]),
        )

    task = service(translation_db).submit("session", request(ids, text=f"{first} {continuation}"))

    assert task.status == "queued"
    assert task.selected_text == f"{first} {continuation}"


def test_rejects_selection_that_skips_an_intervening_prose_block(translation_db) -> None:
    database, ids = translation_db
    first = "The first paragraph contains enough words to count as normal running prose."
    intervening = "This intervening sentence is also normal running prose and cannot be skipped."
    continuation = "The final paragraph also contains enough words to remain protected by validation."
    blocks = [
        {"block_id": "0:1", "kind": "text", "text": first},
        {"block_id": "0:2", "kind": "text", "text": intervening},
        {"block_id": "0:3", "kind": "text", "text": continuation},
    ]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET blocks_json=? WHERE parse_run_id=?",
            (json.dumps(blocks), ids["run"]),
        )

    with pytest.raises(SelectionRejected, match="selection_text_mismatch"):
        service(translation_db).submit("session", request(ids, text=f"{first} {continuation}"))


def test_rejects_selection_that_mixes_page_words_with_forged_text(translation_db) -> None:
    _, ids = translation_db

    with pytest.raises(SelectionRejected, match="selection_text_mismatch"):
        service(translation_db).submit(
            "session",
            request(ids, text="selected words entirely forged trailing"),
        )


def test_accepts_reordered_capture_when_tokens_stay_in_one_window(translation_db) -> None:
    database, ids = translation_db
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET normalized_text=?,raw_text=? WHERE parse_run_id=?",
            (
                "one two three four five six seven eight nine ten eleven twelve",
                "one two three four five six seven eight nine ten eleven twelve",
                ids["run"],
            ),
        )

    reordered = "three four five seven six eight nine ten"
    task = service(translation_db).submit("session", request(ids, text=reordered))

    assert task.status == "queued"
    assert task.selected_text == reordered


def test_parse_pending_when_content_has_no_completed_parse_run(translation_db) -> None:
    database, ids = translation_db
    with database.transaction() as connection:
        connection.execute(
            "UPDATE parse_runs SET status='failed',error_code='invalid_pdf_structure' WHERE id=?",
            (ids["run"],),
        )
        connection.execute(
            "UPDATE contents SET current_parse_run_id=NULL WHERE sha256=?",
            (ids["content"],),
        )

    with pytest.raises(SelectionRejected, match="selection_parse_pending"):
        service(translation_db).submit("session", request(ids))


def test_accepts_pdf_style_span_gap_before_punctuation(translation_db) -> None:
    database, ids = translation_db
    block_text = "The silver-backed chevrotain Tragulus versicolor, a rare species."
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET blocks_json=? WHERE parse_run_id=?",
            (
                '[{"block_id":"0:1","kind":"text","text":'
                '"The silver-backed chevrotain Tragulus versicolor, a rare species.",'
                '"bbox_1000":[0,0,1000,100],"source":"pymupdf",'
                '"confidence":null}]',
                ids["run"],
            ),
        )

    task = service(translation_db).submit(
        "session",
        request(
            ids,
            text="The silver-backed chevrotain Tragulus versicolor , a rare species.",
        ),
    )

    assert task.status == "queued"
    assert task.selected_text == block_text


def test_accepts_missing_space_before_styled_scientific_name(translation_db) -> None:
    database, ids = translation_db
    block_text = "The silver-backed chevrotain Tragulus versicolor, a rare species."
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET blocks_json=? WHERE parse_run_id=?",
            (
                '[{"block_id":"0:1","kind":"text","text":'
                '"The silver-backed chevrotain Tragulus versicolor, a rare species.",'
                '"bbox_1000":[0,0,1000,100],"source":"pymupdf",'
                '"confidence":null}]',
                ids["run"],
            ),
        )

    task = service(translation_db).submit(
        "session",
        request(
            ids,
            text="The silver-backed chevrotainTragulus versicolor, a rare species.",
        ),
    )

    assert task.status == "queued"
    assert task.selected_text == block_text


def test_recovers_multiple_missing_pdf_whitespace_boundaries(translation_db) -> None:
    database, ids = translation_db
    block_text = (
        "Human pressures are perversely intense, widespread and rapidly intensifying."
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE pages SET blocks_json=? WHERE parse_run_id=?",
            (
                '[{"block_id":"0:1","kind":"text","text":'
                '"Human pressures are perversely intense, widespread and rapidly intensifying.",'
                '"bbox_1000":[0,0,1000,100],"source":"pymupdf",'
                '"confidence":null}]',
                ids["run"],
            ),
        )

    task = service(translation_db).submit(
        "session",
        request(
            ids,
            text=(
                "Human pressures are perversely "
                "intense,widespreadandrapidly intensifying."
            ),
        ),
    )

    assert task.status == "queued"
    assert task.selected_text == block_text


def test_online_completion_is_cached_by_provider_model_and_selection(translation_db) -> None:
    _, ids = translation_db
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)

    first = subject.submit("session", request(ids))
    subject.process_task(first.id)
    second = subject.submit("session", request(ids))
    subject.process_task(second.id)

    assert len(deepseek.calls) == 1
    assert subject.get(first.id, session_generation="session").status == "completed"
    cached = subject.get(second.id, session_generation="session")
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
    failed = subject.get(task.id, session_generation="session")
    assert failed.status == "failed"
    assert failed.error_code == code
    assert qwen.calls == []
    assert process.calls == 0


def test_transient_deepseek_failure_is_retried_once_then_succeeds(translation_db) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(qwen_fallback_enabled=True)
    deepseek = FakeDeepSeek(error_sequence=["deepseek_transport_failed", None])
    qwen, process = FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    subject.process_task(task.id)

    completed = subject.get(task.id, session_generation="session")
    assert completed.status == "completed"
    assert completed.provider == "deepseek"
    assert completed.fallback_reason is None
    assert len(deepseek.calls) == 2
    assert qwen.calls == []
    assert process.calls == 0


def test_non_retryable_deepseek_failure_is_not_retried(translation_db) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(qwen_fallback_enabled=True)
    deepseek = FakeDeepSeek(error="deepseek_response_too_large")
    qwen, process = FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    subject.process_task(task.id)

    completed = subject.get(task.id, session_generation="session")
    assert completed.status == "completed"
    assert completed.provider == "qwen"
    assert completed.fallback_reason == "deepseek_response_too_large"
    assert len(deepseek.calls) == 1


def test_known_transient_deepseek_failure_falls_back_to_qwen_once(translation_db) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(qwen_fallback_enabled=True)
    deepseek = FakeDeepSeek(error="deepseek_total_timeout")
    qwen, process = FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    subject.process_task(task.id)

    completed = subject.get(task.id, session_generation="session")
    assert completed.status == "completed"
    assert completed.provider == "qwen"
    assert completed.model == "literature-workbench-qwen"
    assert completed.fallback_reason == "deepseek_total_timeout"
    # One bounded retry precedes the fallback, so DeepSeek sees two attempts.
    assert len(deepseek.calls) == 2
    assert len(qwen.calls) == process.calls == 1


def test_local_only_routes_directly_to_qwen(translation_db) -> None:
    database, ids = translation_db
    SettingsRepository(database).update(default_translation_mode="local_only")
    deepseek, qwen, process = FakeDeepSeek(), FakeQwen(), FakeQwenProcess()
    subject = service(translation_db, deepseek=deepseek, qwen=qwen, process=process)

    task = subject.submit("session", request(ids))
    subject.process_task(task.id)

    assert subject.get(task.id, session_generation="session").provider == "qwen"
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


def test_stale_completion_cannot_overwrite_a_failed_translation(translation_db) -> None:
    database, ids = translation_db
    repository = TranslationRepository(database, lambda: datetime.now(UTC))
    task = service(translation_db).submit("session", request(ids))
    repository.mark_running(task.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE translation_tasks SET status='failed',error_code='newer_failure' WHERE id=?",
            (str(task.id),),
        )

    result = repository.complete(
        task.id,
        cache_key="b" * 64,
        translated_text="stale result",
        detected_source_language="und",
        provider="deepseek",
        model="deepseek-chat",
        fallback_reason=None,
    )

    assert result.status == "failed"
    assert result.error_code == "newer_failure"


def test_stale_cached_completion_cannot_overwrite_a_failed_translation(
    translation_db,
) -> None:
    database, ids = translation_db
    repository = TranslationRepository(database, lambda: datetime.now(UTC))
    task = service(translation_db).submit("session", request(ids))
    repository.mark_running(task.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE translation_tasks SET status='failed',error_code='newer_failure' WHERE id=?",
            (str(task.id),),
        )

    result = repository.complete_cached(
        task.id,
        CachedTranslation(
            cache_key="c" * 64,
            translated_text="cached result",
            detected_source_language="und",
            provider="qwen",
            model="literature-workbench-qwen",
        ),
    )

    assert result.status == "failed"
    assert result.error_code == "newer_failure"


def test_stale_failure_cannot_overwrite_a_completed_translation(translation_db) -> None:
    database, ids = translation_db
    repository = TranslationRepository(database, lambda: datetime.now(UTC))
    task = service(translation_db).submit("session", request(ids))
    repository.mark_running(task.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE translation_tasks SET status='completed',stage='completed' WHERE id=?",
            (str(task.id),),
        )

    result = repository.fail(task.id, "stale_failure")

    assert result.status == "completed"
    assert result.error_code is None


def test_session_scoped_lookup_hides_another_sessions_task(translation_db) -> None:
    _, ids = translation_db
    subject = service(translation_db)
    task = subject.submit("old-session", request(ids))

    with pytest.raises(TranslationNotFound, match="translation_not_found"):
        subject.get(task.id, session_generation="new-session")


def test_same_content_older_document_version_is_not_current(translation_db) -> None:
    database, ids = translation_db
    old_request = request(ids)
    with database.connect() as connection:
        observed = str(connection.execute(
            "SELECT observed_at FROM document_versions WHERE id = ?", (ids["version"],)
        ).fetchone()["observed_at"])
    later = (datetime.fromisoformat(observed) + timedelta(seconds=1)).isoformat()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO document_versions(
               id,document_id,content_sha256,size_bytes,mtime_ns,volume_id,file_id,
               hard_link_count,observed_at) VALUES(?,?,?,12,2,'vol','file-new',1,?)""",
            (
                str(uuid4()),
                ids["document"],
                ids["content"],
                later,
            ),
        )

    with pytest.raises(SelectionRejected, match="selection_version_stale"):
        service(translation_db).submit("session", old_request)


def _page_text(database: Database, text: str) -> None:
    with database.transaction() as connection:
        connection.execute("UPDATE pages SET raw_text=?, normalized_text=?", (text, text))


def _annotation_revision(database: Database, ids: dict[str, str]) -> dict[str, str]:
    newer = {**ids, "version": str(uuid4()), "content": "b" * 64, "run": str(uuid4())}
    now = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    with database.transaction() as c:
        c.execute("""INSERT INTO contents(sha256,size_bytes,mime_type,page_count,current_parse_run_id,status,created_at,updated_at)
                     VALUES(?,12,'application/pdf',1,NULL,'completed',?,?)""", (newer["content"], now, now))
        c.execute("INSERT INTO document_versions VALUES(?,?,?,12,1,'vol','file',1,?)", (newer["version"], ids["document"], newer["content"], now))
        c.execute("""INSERT INTO parse_runs(id,content_sha256,parser_name,parser_version,status,created_at,updated_at,completed_at)
                     VALUES(?,?,'pymupdf','1','completed',?,?,?)""", (newer["run"], newer["content"], now, now, now))
        c.execute("UPDATE contents SET current_parse_run_id=? WHERE sha256=?", (newer["run"], newer["content"]))
        c.execute("""INSERT INTO pages SELECT ?,page_index,physical_page,?,normalized_text,raw_text,width_points,height_points,blocks_json,coordinate_reliable
                     FROM pages WHERE content_sha256=?""", (newer["content"], newer["run"], ids["content"]))
        c.execute("UPDATE documents SET content_sha256=? WHERE id=?", (newer["content"], ids["document"]))
    return newer


def test_annotation_revision_reuses_translation_without_cloud_call(translation_db) -> None:
    database, ids = translation_db
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids)).id)
    newer = _annotation_revision(database, ids)
    second = subject.process_task(subject.submit("session", request(newer)).id)
    assert len(deepseek.calls) == 1
    assert second.reuse_details["mode"] == "exact"
    assert second.reuse_details["cloud_requested"] is False
    assert second.translated_text == "深度求索译文"


def test_changed_page_text_does_not_reuse_old_translation(translation_db) -> None:
    database, ids = translation_db
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids)).id)
    newer = _annotation_revision(database, ids)
    with database.transaction() as c:
        c.execute("UPDATE pages SET normalized_text=?,raw_text=? WHERE content_sha256=?", ("Different evidence selected words", "Different evidence selected words", newer["content"]))
    second = subject.process_task(subject.submit("session", request(newer)).id)
    assert len(deepseek.calls) == 2
    assert second.reuse_details["mode"] == "fresh"


def test_legacy_translation_is_reused_after_annotation(translation_db) -> None:
    database, ids = translation_db
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    first = subject.process_task(subject.submit("session", request(ids)).id)
    with database.transaction() as c:
        c.execute("DELETE FROM translation_memory")
        c.execute("UPDATE translation_tasks SET page_text_hash='',reuse_details='{}' WHERE id=?", (str(first.id),))
    newer = _annotation_revision(database, ids)
    second = subject.process_task(subject.submit("session", request(newer)).id)
    assert len(deepseek.calls) == 1
    assert second.reuse_details["mode"] == "exact"


def test_shrinking_selection_is_labelled_reference_and_exact_is_opt_in(translation_db) -> None:
    _, ids = translation_db
    deepseek = FakeDeepSeek(result="整段译文")
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids, text="Alpha beta selected words gamma delta")).id)
    short = subject.process_task(subject.submit("session", request(ids)).id)
    assert short.reuse_details["mode"] == "reference"
    assert short.reuse_details["source_text"] == "Alpha beta selected words gamma delta"
    assert short.translated_text == "整段译文"
    assert len(deepseek.calls) == 1
    deepseek.result = "精确短句译文"
    exact = subject.process_task(subject.submit("session", replace(request(ids), prefer_exact=True)).id)
    assert exact.translated_text == "精确短句译文"
    assert exact.reuse_details["mode"] == "fresh"
    again = subject.process_task(subject.submit("session", request(ids)).id)
    assert again.translated_text == "精确短句译文"
    assert again.reuse_details["mode"] == "exact"
    assert len(deepseek.calls) == 2


def test_expanding_selection_translates_only_new_complete_sentence(translation_db) -> None:
    database, ids = translation_db
    old, new = "Animals increased.", "Habitats improved."
    _page_text(database, f"{old} {new}")
    deepseek = FakeDeepSeek(result="动物增加。")
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids, text=old)).id)
    deepseek.result = "栖息地改善。"
    expanded = subject.process_task(subject.submit("session", request(ids, text=f"{old} {new}")).id)
    assert len(deepseek.calls) == 2
    assert deepseek.calls[-1].verified_text == new
    assert expanded.translated_text == "动物增加。\n\n栖息地改善。"
    assert expanded.reuse_details["mode"] == "partial"
    assert [p["cached"] for p in expanded.reuse_details["parts"]] == [True, False]
    subject.process_task(subject.submit("session", request(ids, text=new)).id)
    assert len(deepseek.calls) == 2


@pytest.mark.parametrize("prefix", ["The U.S.", "See Fig.", "Named by Dr.", "Species sp.", 'The "U.K."'])
def test_abbreviation_expansion_translates_the_whole_sentence(translation_db, prefix) -> None:
    database, ids = translation_db
    text = f"{prefix} population increased."
    _page_text(database, text)
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids, text=prefix)).id)
    expanded = subject.process_task(subject.submit("session", request(ids, text=text)).id)
    assert expanded.reuse_details["mode"] == "fresh"
    assert len(deepseek.calls) == 2
    assert deepseek.calls[-1].verified_text == text


def test_changed_negation_is_not_translated_by_pasting_cached_words(translation_db) -> None:
    database, ids = translation_db
    _page_text(database, "The result was not significant.")
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids, text="significant.")).id)
    second = subject.process_task(subject.submit("session", request(ids, text="not significant.")).id)
    assert len(deepseek.calls) == 2
    assert deepseek.calls[-1].verified_text == "not significant."
    assert second.reuse_details["mode"] == "fresh"


def test_successful_new_fragment_survives_later_failure(translation_db) -> None:
    database, ids = translation_db
    text = "First sentence. Cached sentence. Last sentence."
    _page_text(database, text)
    deepseek = FakeDeepSeek()
    subject = service(translation_db, deepseek=deepseek)
    subject.process_task(subject.submit("session", request(ids, text="Cached sentence.")).id)
    deepseek.error_sequence = [None, "deepseek_authorization_failed"]
    failed = subject.process_task(subject.submit("session", request(ids, text=text)).id)
    assert failed.status == "failed"
    assert failed.reuse_details["cloud_requested"] is True
    assert len(deepseek.calls) == 3
    retry = subject.process_task(subject.submit("session", request(ids, text=text)).id)
    assert retry.status == "completed"
    assert len(deepseek.calls) == 4
    assert deepseek.calls[-1].verified_text == "Last sentence."


def test_translation_memory_separates_document_page_model_and_language(translation_db) -> None:
    database, ids = translation_db
    subject = service(translation_db)
    task = subject.process_task(subject.submit("session", request(ids)).id)
    repo = TranslationRepository(database, lambda: datetime.now(UTC))
    assert repo.memories(task, "deepseek", task.model)
    assert not repo.memories(replace(task, document_id=uuid4()), "deepseek", task.model)
    assert not repo.memories(replace(task, physical_page=2), "deepseek", task.model)
    assert not repo.memories(task, "deepseek", "another-model")
    assert not repo.memories(task, "qwen", task.model)


def test_queued_translation_rejects_changed_parse_before_cache_hit(translation_db) -> None:
    database, ids = translation_db
    subject = service(translation_db)
    subject.process_task(subject.submit("session", request(ids)).id)
    queued = subject.submit("session", request(ids))
    _page_text(database, "Changed page containing selected words")
    assert subject.process_task(queued.id).status == "failed"
