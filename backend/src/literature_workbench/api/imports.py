from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from literature_workbench.features.library.import_intents import ImportTask


router = APIRouter(prefix="/api/imports", tags=["imports"])


class CreateImportIntent(BaseModel):
    original_name: str
    size: int = Field(gt=0, le=2 * 1024**3)
    media_type: Literal["application/pdf"]


class ImportIntentResult(BaseModel):
    intent_id: UUID
    upload_token: str
    expires_at: datetime


def _view(task: ImportTask) -> dict[str, object]:
    return {
        "intent_id": str(task.intent_id),
        "status": task.status,
        "document_id": None if task.document_id is None else str(task.document_id),
        "error_code": task.error_code,
        "poll_after_ms": task.poll_after_ms,
    }


@router.post("/intents", status_code=201, response_model=ImportIntentResult)
def create_intent(request: Request, payload: CreateImportIntent) -> ImportIntentResult:
    created = request.app.state.import_intent_repository.create(
        request.app.state.local_session.session_generation,
        payload.original_name,
        payload.size,
        payload.media_type,
    )
    return ImportIntentResult(
        intent_id=created.intent_id,
        upload_token=created.upload_token,
        expires_at=created.expires_at,
    )


@router.post("/{intent_id}/content", status_code=202)
async def upload_content(
    request: Request,
    intent_id: UUID,
    x_lw_upload_token: str = Header(default=""),
) -> JSONResponse:
    if request.headers.get("content-type") != "application/pdf":
        return JSONResponse({"code": "unsupported_media_type"}, status_code=415)
    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        return JSONResponse({"code": "content_length_required"}, status_code=411)
    declared_length = int(raw_length)
    if declared_length > 2 * 1024**3:
        return JSONResponse({"code": "import_too_large"}, status_code=413)
    try:
        result = await request.app.state.import_service.consume(
            intent_id,
            x_lw_upload_token,
            request.stream(),
            declared_length=declared_length,
        )
    except KeyError:
        return JSONResponse({"code": "import_not_found"}, status_code=404)
    except ValueError as error:
        return JSONResponse({"code": str(error)}, status_code=422)
    return JSONResponse(_view(result), status_code=202)


@router.get("")
def list_imports(request: Request, active: bool = True) -> JSONResponse:
    del active
    tasks = request.app.state.import_intent_repository.list_current(
        request.app.state.local_session.session_generation
    )
    return JSONResponse({"items": [_view(task) for task in tasks]})


@router.get("/{intent_id}")
def get_import(request: Request, intent_id: UUID) -> JSONResponse:
    try:
        task = request.app.state.import_intent_repository.get(
            intent_id, request.app.state.local_session.session_generation
        )
    except KeyError:
        return JSONResponse({"code": "import_not_found"}, status_code=404)
    return JSONResponse(_view(task))
