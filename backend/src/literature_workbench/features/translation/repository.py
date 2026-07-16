from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue


PROMPT_VERSION = "selection-translation-v1"
MAX_CONTEXT_CODE_POINTS = 500
TranslationStatus = Literal["queued", "running", "completed", "failed"]


class SelectionRejected(ValueError):
    pass


class TranslationNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedSelection:
    document_id: UUID
    document_version_id: UUID
    content_id: str
    physical_page: int
    selected_text: str
    context_before: str
    context_after: str
    target_language: Literal["zh-CN"]


@dataclass(frozen=True, slots=True)
class TranslationTask:
    id: UUID
    client_task_id: UUID
    document_id: UUID
    document_version_id: UUID
    content_id: str
    physical_page: int
    selected_text: str
    context_before: str
    context_after: str
    target_language: Literal["zh-CN"]
    route_mode: Literal["online", "local_only"]
    requested_provider: str
    requested_model: str
    settings_revision: int
    prompt_version: str
    status: TranslationStatus
    provider: str | None
    model: str | None
    fallback_reason: str | None
    result_cache_key: str | None
    error_code: str | None
    translated_text: str | None
    detected_source_language: str | None


@dataclass(frozen=True, slots=True)
class CachedTranslation:
    cache_key: str
    translated_text: str
    detected_source_language: str
    provider: str
    model: str


def normalize_selection(value: str) -> str:
    return re.sub(r"\s+", " ", value, flags=re.UNICODE).strip()


def current_page_text_candidates(row: sqlite3.Row) -> list[str]:
    candidates = [
        normalize_selection(str(row["normalized_text"] or row["raw_text"])),
        normalize_selection(str(row["raw_text"])),
    ]
    try:
        raw_blocks = json.loads(str(row["blocks_json"]))
    except (json.JSONDecodeError, TypeError):
        raw_blocks = []
    block_texts = [
        normalize_selection(block["text"])
        for block in raw_blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ] if isinstance(raw_blocks, list) else []
    candidates.extend(text for text in block_texts if text)
    joined_blocks = normalize_selection(" ".join(block_texts))
    if joined_blocks:
        candidates.append(joined_blocks)
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


class TranslationRepository:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self._database = database
        self._now = now

    def verify_selection(
        self,
        *,
        document_id: UUID,
        document_version_id: UUID,
        content_id: str,
        physical_page: int,
        selected_text: str,
        target_language: Literal["zh-CN"],
    ) -> VerifiedSelection:
        normalized_selection = normalize_selection(selected_text)
        if not normalized_selection or len(normalized_selection) > 8_000:
            raise SelectionRejected("selection_not_current")
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.raw_text,p.normalized_text,p.blocks_json
                FROM documents AS d
                JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
                JOIN document_versions AS v
                  ON v.id=? AND v.document_id=d.id AND v.content_sha256=d.content_sha256
                 AND v.id=(
                   SELECT latest.id FROM document_versions AS latest
                   WHERE latest.document_id=d.id
                   ORDER BY latest.observed_at DESC,latest.rowid DESC LIMIT 1
                 )
                JOIN contents AS c
                  ON c.sha256=d.content_sha256 AND c.mime_type='application/pdf'
                JOIN parse_runs AS r
                  ON r.id=c.current_parse_run_id
                 AND r.content_sha256=c.sha256
                 AND r.status='completed'
                JOIN pages AS p
                  ON p.parse_run_id=r.id
                 AND p.content_sha256=c.sha256
                 AND p.physical_page=?
                WHERE d.id=? AND d.content_sha256=? AND d.format='pdf'
                  AND d.status IN ('candidate_pdf','valid_pdf')
                """,
                (
                    str(document_version_id),
                    physical_page,
                    str(document_id),
                    content_id,
                ),
            ).fetchone()
        if row is None:
            raise SelectionRejected("selection_not_current")
        matching_text = next(
            (
                candidate
                for candidate in current_page_text_candidates(row)
                if normalized_selection in candidate
            ),
            None,
        )
        if matching_text is None:
            raise SelectionRejected("selection_not_current")
        position = matching_text.find(normalized_selection)
        before = matching_text[:position].rstrip()
        after = matching_text[position + len(normalized_selection) :].lstrip()
        return VerifiedSelection(
            document_id=document_id,
            document_version_id=document_version_id,
            content_id=content_id,
            physical_page=physical_page,
            selected_text=normalized_selection,
            context_before="".join(list(before)[-MAX_CONTEXT_CODE_POINTS:]),
            context_after="".join(list(after)[:MAX_CONTEXT_CODE_POINTS]),
            target_language=target_language,
        )

    def create(
        self,
        *,
        session_generation: str,
        client_task_id: UUID,
        selection: VerifiedSelection,
        route_mode: Literal["online", "local_only"],
        requested_provider: str,
        requested_model: str,
        settings_revision: int,
        queue: TaskQueue,
    ) -> TranslationTask:
        timestamp = self._timestamp()
        task_id = uuid4()
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM translation_tasks WHERE session_generation=? AND client_task_id=?",
                (session_generation, str(client_task_id)),
            ).fetchone()
            if existing is not None:
                return self.get_for_worker(UUID(str(existing["id"])))
            connection.execute(
                """
                INSERT INTO translation_tasks(
                  id,session_generation,client_task_id,document_id,document_version_id,
                  content_sha256,physical_page,selected_text,context_before,context_after,
                  target_language,route_mode,requested_provider,requested_model,
                  settings_revision,prompt_version,status,stage,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued','pending',?,?)
                """,
                (
                    str(task_id), session_generation, str(client_task_id),
                    str(selection.document_id), str(selection.document_version_id),
                    selection.content_id, selection.physical_page, selection.selected_text,
                    selection.context_before, selection.context_after,
                    selection.target_language, route_mode, requested_provider,
                    requested_model, settings_revision, PROMPT_VERSION, timestamp, timestamp,
                ),
            )
            queue.enqueue_in_transaction(
                connection,
                "translate_selection",
                f"translation:{task_id}",
                {
                    "task_id": str(task_id),
                    "document_id": str(selection.document_id),
                    "document_version_id": str(selection.document_version_id),
                    "content_id": selection.content_id,
                    "physical_page": selection.physical_page,
                    "selected_text": selection.selected_text,
                    "target_language": selection.target_language,
                    "prompt_version": PROMPT_VERSION,
                },
            )
        return self.get_for_worker(task_id)

    def get(
        self, task_id: UUID, *, session_generation: str
    ) -> TranslationTask:
        return self._get(task_id, session_generation=session_generation)

    def get_for_worker(self, task_id: UUID) -> TranslationTask:
        return self._get(task_id, session_generation=None)

    def _get(
        self, task_id: UUID, *, session_generation: str | None
    ) -> TranslationTask:
        session_clause = "" if session_generation is None else " AND t.session_generation=?"
        parameters = (
            (str(task_id),)
            if session_generation is None
            else (str(task_id), session_generation)
        )
        with self._database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT t.*,c.translated_text,c.detected_source_language
                FROM translation_tasks AS t
                LEFT JOIN translation_cache AS c ON c.cache_key=t.result_cache_key
                WHERE t.id=?{session_clause}
                """,  # noqa: S608 - clause is selected only from fixed server literals
                parameters,
            ).fetchone()
        if row is None:
            raise TranslationNotFound("translation_not_found")
        return self._task_from_row(row)

    def mark_running(self, task_id: UUID) -> TranslationTask:
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE translation_tasks SET status='running',stage='model_request',
                   updated_at=? WHERE id=? AND status='queued'""",
                (self._timestamp(), str(task_id)),
            )
        return self.get_for_worker(task_id)

    def cache_key(self, task: TranslationTask, provider: str, model: str) -> str:
        stable = json.dumps(
            {
                "document_version_id": str(task.document_version_id),
                "content_id": task.content_id,
                "physical_page": task.physical_page,
                "selected_text": normalize_selection(task.selected_text),
                "target_language": task.target_language,
                "prompt_version": task.prompt_version,
                "provider": provider,
                "model": model,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(stable).hexdigest()

    def cached(self, cache_key: str) -> CachedTranslation | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM translation_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        return CachedTranslation(
            cache_key=cache_key,
            translated_text=str(row["translated_text"]),
            detected_source_language=str(row["detected_source_language"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
        )

    def complete(
        self,
        task_id: UUID,
        *,
        cache_key: str,
        translated_text: str,
        detected_source_language: str,
        provider: str,
        model: str,
        fallback_reason: str | None,
    ) -> TranslationTask:
        task = self.get_for_worker(task_id)
        timestamp = self._timestamp()
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO translation_cache(
                   cache_key,translated_text,detected_source_language,target_language,
                   provider,model,prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (cache_key, translated_text, detected_source_language, task.target_language,
                 provider, model, task.prompt_version, timestamp),
            )
            connection.execute(
                """UPDATE translation_tasks SET status='completed',stage='completed',
                   provider=?,model=?,fallback_reason=?,result_cache_key=?,error_code=NULL,
                   updated_at=? WHERE id=?""",
                (provider, model, fallback_reason, cache_key, timestamp, str(task_id)),
            )
        return self.get_for_worker(task_id)

    def complete_cached(
        self, task_id: UUID, cached: CachedTranslation, fallback_reason: str | None = None
    ) -> TranslationTask:
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE translation_tasks SET status='completed',stage='cache_hit',
                   provider=?,model=?,fallback_reason=?,result_cache_key=?,error_code=NULL,
                   updated_at=? WHERE id=?""",
                (cached.provider, cached.model, fallback_reason, cached.cache_key,
                 self._timestamp(), str(task_id)),
            )
        return self.get_for_worker(task_id)

    def fail(self, task_id: UUID, error_code: str) -> TranslationTask:
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE translation_tasks SET status='failed',stage='failed',error_code=?,
                   updated_at=? WHERE id=?""",
                (error_code, self._timestamp(), str(task_id)),
            )
        return self.get_for_worker(task_id)

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TranslationTask:
        return TranslationTask(
            id=UUID(str(row["id"])), client_task_id=UUID(str(row["client_task_id"])),
            document_id=UUID(str(row["document_id"])),
            document_version_id=UUID(str(row["document_version_id"])),
            content_id=str(row["content_sha256"]), physical_page=int(row["physical_page"]),
            selected_text=str(row["selected_text"]), context_before=str(row["context_before"]),
            context_after=str(row["context_after"]), target_language="zh-CN",
            route_mode=str(row["route_mode"]),  # type: ignore[arg-type]
            requested_provider=str(row["requested_provider"]),
            requested_model=str(row["requested_model"]),
            settings_revision=int(row["settings_revision"]), prompt_version=str(row["prompt_version"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            provider=None if row["provider"] is None else str(row["provider"]),
            model=None if row["model"] is None else str(row["model"]),
            fallback_reason=None if row["fallback_reason"] is None else str(row["fallback_reason"]),
            result_cache_key=None if row["result_cache_key"] is None else str(row["result_cache_key"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            translated_text=None if row["translated_text"] is None else str(row["translated_text"]),
            detected_source_language=None if row["detected_source_language"] is None else str(row["detected_source_language"]),
        )
