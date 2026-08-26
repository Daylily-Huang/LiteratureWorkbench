from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from literature_workbench.features.translation.repository import (
    SelectionRejected,
    TranslationNotFound,
    TranslationTask,
)
from literature_workbench.features.translation.service import (
    TranslationRequest,
    TranslationService,
)


router = APIRouter(prefix="/api/translations", tags=["translations"])


class TranslationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_task_id: UUID
    document_id: UUID
    document_version_id: UUID
    content_id: str = Field(min_length=1, max_length=128)
    physical_page: int = Field(ge=1)
    selected_text: str = Field(min_length=1, max_length=8_000)
    target_language: Literal["zh-CN"]


def _service(request: Request) -> TranslationService:
    return cast(TranslationService, request.app.state.translation_service)


def _body(task: TranslationTask) -> dict[str, object]:
    return {
        "id": str(task.id),
        "client_task_id": str(task.client_task_id),
        "status": task.status,
        "selected_text": task.selected_text,
        "translated_text": task.translated_text,
        "source_language": task.detected_source_language,
        "target_language": task.target_language,
        "provider": task.provider,
        "model": task.model,
        "fallback_reason": task.fallback_reason,
        "error_code": task.error_code,
    }


@router.post("")
def create_translation(request: Request, payload: TranslationCreate) -> JSONResponse:
    try:
        task = _service(request).submit(
            request.app.state.local_session.session_generation,
            TranslationRequest(**payload.model_dump()),
        )
    except SelectionRejected as error:
        return JSONResponse({"code": str(error)}, status_code=409)
    return JSONResponse(_body(task), status_code=202 if task.status == "queued" else 200)


@router.get("/{task_id}")
def get_translation(request: Request, task_id: UUID) -> JSONResponse:
    try:
        task = _service(request).get(
            task_id,
            session_generation=request.app.state.local_session.session_generation,
        )
    except TranslationNotFound:
        return JSONResponse({"code": "translation_not_found"}, status_code=404)
    return JSONResponse(_body(task))
