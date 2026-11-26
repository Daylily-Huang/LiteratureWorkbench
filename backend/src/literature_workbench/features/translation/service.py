from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from uuid import UUID

from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.egress.models import VerifiedSelectionProof
from literature_workbench.features.models.qwen_local import QwenLocalError
from literature_workbench.features.models.qwen_process import (
    QWEN_ALIAS,
    QwenProcessError,
)
from literature_workbench.features.settings.repository import SettingsRepository
from literature_workbench.features.translation.prompt import (
    build_academic_translation_prompt,
    effective_prompt_version,
)
from literature_workbench.features.translation.repository import (
    PROMPT_VERSION,
    SelectionRejected,
    TranslationRepository,
    TranslationTask,
)

from literature_workbench.features.translation.reuse import TranslationPiece, reuse_plan


_FALLBACK_ERRORS = {
    "deepseek_http_error",
    "deepseek_transport_failed",
    "deepseek_total_timeout",
    "deepseek_response_too_large",
    "invalid_deepseek_response",
}

# Transient transport classes: the connection stalled or the server returned a
# retryable status. A single bounded retry on a fresh request usually succeeds
# within seconds, so it runs before the (much slower) local-model fallback.
# Non-retryable classes (credentials, authorization, request/response contract)
# fail immediately.
_RETRYABLE_ERRORS = {
    "deepseek_http_error",
    "deepseek_transport_failed",
    "deepseek_total_timeout",
}


class DeepSeekClient(Protocol):
    def translate_selection(self, proof: VerifiedSelectionProof) -> str: ...


class QwenClient(Protocol):
    def translate(self, prompt: str) -> str: ...


class QwenProcess(Protocol):
    def ensure_running(self) -> object: ...


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    client_task_id: UUID
    document_id: UUID
    document_version_id: UUID
    content_id: str
    physical_page: int
    selected_text: str
    target_language: Literal["zh-CN"]
    prefer_exact: bool = False


class TranslationService:
    def __init__(
        self,
        repository: TranslationRepository,
        settings: SettingsRepository,
        queue: TaskQueue,
        deepseek: DeepSeekClient,
        qwen: QwenClient,
        qwen_process: QwenProcess,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._queue = queue
        self._deepseek = deepseek
        self._qwen = qwen
        self._qwen_process = qwen_process

    def submit(self, session_generation: str, request: TranslationRequest) -> TranslationTask:
        selection = self._repository.verify_selection(
            document_id=request.document_id,
            document_version_id=request.document_version_id,
            content_id=request.content_id,
            physical_page=request.physical_page,
            selected_text=request.selected_text,
            target_language=request.target_language,
        )
        settings = self._settings.read()
        provider = "qwen" if settings.default_translation_mode == "local_only" else "deepseek"
        model = QWEN_ALIAS if provider == "qwen" else settings.deepseek_model
        return self._repository.create(
            session_generation=session_generation,
            client_task_id=request.client_task_id,
            selection=selection,
            route_mode=settings.default_translation_mode,
            requested_provider=provider,
            requested_model=model,
            settings_revision=settings.revision,
            queue=self._queue,
            prefer_exact=request.prefer_exact,
            prompt_version=effective_prompt_version(PROMPT_VERSION, settings.translation_glossary),
        )

    def get(
        self, task_id: UUID, *, session_generation: str
    ) -> TranslationTask:
        return self._repository.get(task_id, session_generation=session_generation)

    def process_task(self, task_id: UUID) -> TranslationTask:
        task = self._repository.get_for_worker(task_id)
        if task.status in {"completed", "failed"}:
            return task
        if task.status == "running":
            return self._repository.fail(task.id, "translation_send_ambiguous")
        task = self._repository.mark_running(task_id)
        try:
            verified = self._repository.verify_selection(
                document_id=task.document_id, document_version_id=task.document_version_id,
                content_id=task.content_id, physical_page=task.physical_page,
                selected_text=task.selected_text, target_language=task.target_language,
            )
            if task.page_text_hash and verified.page_text_hash != task.page_text_hash:
                raise SelectionRejected("selection_not_current")
        except SelectionRejected as error:
            return self._repository.fail(task.id, str(error))
        if task.requested_provider == "qwen":
            return self._run_qwen(task, fallback_reason=None)
        try:
            return self._translate_reusing(task, "deepseek", task.requested_model)
        except EgressRejected as error:
            code = str(error)
            current_settings = self._settings.read()
            if code in _FALLBACK_ERRORS and current_settings.qwen_fallback_enabled:
                return self._run_qwen(task, fallback_reason=code)
            return self._repository.fail(task.id, code or "deepseek_failed")

    def _translate_with_one_retry(self, proof: VerifiedSelectionProof) -> str:
        try:
            return self._deepseek.translate_selection(proof)
        except EgressRejected as first_error:
            if str(first_error) not in _RETRYABLE_ERRORS:
                raise
        # The gateway audits every attempt separately (attempt_no), so the
        # retry leaves a complete trail before any fallback decision.
        return self._deepseek.translate_selection(proof)

    def _run_qwen(self, task: TranslationTask, fallback_reason: str | None) -> TranslationTask:
        try:
            return self._translate_reusing(task, "qwen", QWEN_ALIAS, fallback_reason)
        except (QwenProcessError, QwenLocalError) as error:
            return self._repository.fail(task.id, str(error) or "qwen_failed")
        except (OSError, RuntimeError):
            return self._repository.fail(task.id, "qwen_failed")

    def _translate_reusing(
        self, task: TranslationTask, provider: str, model: str, fallback_reason: str | None = None,
    ) -> TranslationTask:
        mode, pieces = reuse_plan(
            task.selected_text, self._repository.memories(task, provider, model),
            prefer_exact=task.prefer_exact,
        )
        cloud_requested = bool(self._repository.get_for_worker(task.id).reuse_details.get("cloud_requested"))
        details: dict[str, object] = {
            "mode": mode, "cloud_requested": cloud_requested,
            "reused_characters": sum(len(p.source) for p in pieces if p.translation is not None),
            "new_characters": sum(len(p.source) for p in pieces if p.translation is None),
            "source_text": pieces[0].source if mode == "reference" else task.selected_text,
            "parts": [],
        }
        self._repository.set_reuse_details(task.id, details)
        finished: list[TranslationPiece] = []
        for piece in pieces:
            cached = piece.translation is not None
            if not cached:
                if provider == "deepseek":
                    details["cloud_requested"] = True
                    self._repository.set_reuse_details(task.id, details)
                    translated = self._translate_with_one_retry(VerifiedSelectionProof(
                        task_id=task.id, document_id=task.document_id,
                        document_version_id=task.document_version_id, content_sha256=task.content_id,
                        physical_page=task.physical_page, verified_text=piece.source,
                        target_language=task.target_language, model=model,
                    ))
                else:
                    self._qwen_process.ensure_running()
                    paper_title, paper_opening = self._repository.read_paper_context(
                        task.document_id, task.content_id, task.physical_page
                    )
                    glossary = self._settings.read().translation_glossary
                    translated = self._qwen.translate(
                        build_academic_translation_prompt(
                            selected_text=piece.source,
                            target_language=task.target_language,
                            paper_title=paper_title,
                            paper_opening=paper_opening,
                            context_before=task.context_before,
                            context_after=task.context_after,
                            glossary=glossary,
                        )
                    )
                piece = replace(piece, translation=translated)
                # Keep successful fragments even if a later new fragment fails.
                self._repository.remember(task, provider, model, piece)
            finished.append(piece)
            details["parts"] = [
                {"source": result.source, "translation": result.translation,
                 "cached": original.translation is not None}
                for result, original in zip(finished, pieces, strict=False)
            ]
            self._repository.set_reuse_details(task.id, details)
        translated_text = "\n\n".join(p.translation or "" for p in finished)
        key = self._repository.cache_key(task, provider, model)
        if mode == "reference":
            # A covering paragraph is not an exact translation of the short selection.
            key += "-reference-" + hashlib.sha256(pieces[0].source.encode()).hexdigest()
        else:
            self._repository.remember(task, provider, model, TranslationPiece(task.selected_text, translated_text))
        return self._repository.complete(
            task.id, cache_key=key, translated_text=translated_text,
            detected_source_language="und", provider=provider, model=model, fallback_reason=fallback_reason,
        )
