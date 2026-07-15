from __future__ import annotations

from dataclasses import dataclass
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
from literature_workbench.features.translation.repository import (
    CachedTranslation,
    TranslationRepository,
    TranslationTask,
)


_FALLBACK_ERRORS = {
    "deepseek_http_error",
    "deepseek_transport_failed",
    "deepseek_total_timeout",
    "deepseek_response_too_large",
    "invalid_deepseek_response",
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
        if task.requested_provider == "qwen":
            return self._run_qwen(task, fallback_reason=None)

        cached = self._provider_cache(task, "deepseek", task.requested_model)
        if cached is not None:
            return self._repository.complete_cached(task.id, cached)
        proof = VerifiedSelectionProof(
            task_id=task.id,
            document_id=task.document_id,
            document_version_id=task.document_version_id,
            content_sha256=task.content_id,
            physical_page=task.physical_page,
            verified_text=task.selected_text,
            target_language=task.target_language,
            model=task.requested_model,
        )
        try:
            translated = self._deepseek.translate_selection(proof)
        except EgressRejected as error:
            code = str(error)
            current_settings = self._settings.read()
            if code in _FALLBACK_ERRORS and current_settings.qwen_fallback_enabled:
                return self._run_qwen(task, fallback_reason=code)
            return self._repository.fail(task.id, code or "deepseek_failed")
        return self._persist(task, translated, "deepseek", task.requested_model, None)

    def _run_qwen(self, task: TranslationTask, fallback_reason: str | None) -> TranslationTask:
        cached = self._provider_cache(task, "qwen", QWEN_ALIAS)
        if cached is not None:
            return self._repository.complete_cached(task.id, cached, fallback_reason)
        prompt = (
            "Translate the selected PDF text to zh-CN. Return only the translation.\n"
            f"Context before: {task.context_before}\n"
            f"Selection: {task.selected_text}\n"
            f"Context after: {task.context_after}"
        )
        try:
            self._qwen_process.ensure_running()
            translated = self._qwen.translate(prompt)
        except (QwenProcessError, QwenLocalError) as error:
            return self._repository.fail(task.id, str(error) or "qwen_failed")
        except (OSError, RuntimeError):
            return self._repository.fail(task.id, "qwen_failed")
        return self._persist(task, translated, "qwen", QWEN_ALIAS, fallback_reason)

    def _provider_cache(
        self, task: TranslationTask, provider: str, model: str
    ) -> CachedTranslation | None:
        return self._repository.cached(self._repository.cache_key(task, provider, model))

    def _persist(
        self,
        task: TranslationTask,
        translated: str,
        provider: str,
        model: str,
        fallback_reason: str | None,
    ) -> TranslationTask:
        return self._repository.complete(
            task.id,
            cache_key=self._repository.cache_key(task, provider, model),
            translated_text=translated,
            detected_source_language="und",
            provider=provider,
            model=model,
            fallback_reason=fallback_reason,
        )
