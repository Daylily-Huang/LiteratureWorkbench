from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from literature_workbench.features.annotations.service import AnnotationError


router = APIRouter(prefix="/api/documents", tags=["annotations"])


class CreateAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_version_id: UUID
    content_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    type: Literal["highlight", "underline", "comment"]
    quads: list[list[float]] = Field(min_length=1, max_length=200)
    color: Literal["yellow", "green", "blue", "pink", "red", "black"]
    comment: str = Field(default="", max_length=20_000)


class UpdateAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_version_id: UUID
    content_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    color: Literal["yellow", "green", "blue", "pink", "red", "black"] | None = None
    comment: str | None = Field(default=None, max_length=20_000)


class DeleteAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_version_id: UUID
    content_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class RestoreBaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_filename: str = Field(min_length=1, max_length=500)


def _error(error: AnnotationError) -> JSONResponse:
    status = 404 if error.code in {"document_not_found", "annotation_not_found"} else 409
    if error.code.endswith("_invalid"):
        status = 422
    return JSONResponse({"code": error.code}, status_code=status)


@router.get("/{document_id}/annotations")
def list_annotations(request: Request, document_id: UUID) -> Response:
    try:
        return JSONResponse(request.app.state.annotation_service.list_annotations(document_id))
    except AnnotationError as error:
        return _error(error)


@router.post("/{document_id}/annotations", status_code=201)
def create_annotation(
    request: Request, document_id: UUID, payload: CreateAnnotationRequest
) -> Response:
    try:
        result = request.app.state.annotation_service.create(
            document_id,
            document_version_id=payload.document_version_id,
            content_id=payload.content_id,
            page_number=payload.page,
            annotation_type=payload.type,
            quads=payload.quads,
            color=payload.color,
            comment=payload.comment,
        )
        return JSONResponse(result, status_code=201)
    except AnnotationError as error:
        return _error(error)


@router.patch("/{document_id}/annotations/{annotation_id}")
def update_annotation(
    request: Request,
    document_id: UUID,
    annotation_id: str,
    payload: UpdateAnnotationRequest,
) -> Response:
    try:
        return JSONResponse(
            request.app.state.annotation_service.update(
                document_id,
                annotation_id,
                document_version_id=payload.document_version_id,
                content_id=payload.content_id,
                color=payload.color,
                comment=payload.comment,
            )
        )
    except AnnotationError as error:
        return _error(error)


@router.delete("/{document_id}/annotations/{annotation_id}")
def delete_annotation(
    request: Request,
    document_id: UUID,
    annotation_id: str,
    payload: DeleteAnnotationRequest,
) -> Response:
    try:
        return JSONResponse(
            request.app.state.annotation_service.delete(
                document_id,
                annotation_id,
                document_version_id=payload.document_version_id,
                content_id=payload.content_id,
            )
        )
    except AnnotationError as error:
        return _error(error)


@router.post("/{document_id}/annotations/undo")
def undo_annotation(request: Request, document_id: UUID) -> Response:
    try:
        return JSONResponse(request.app.state.annotation_service.undo(document_id))
    except AnnotationError as error:
        return _error(error)


@router.post("/{document_id}/annotations/restore-baseline")
def restore_annotation_baseline(
    request: Request, document_id: UUID, payload: RestoreBaselineRequest
) -> Response:
    try:
        return JSONResponse(
            request.app.state.annotation_service.restore_baseline(
                document_id, confirm_filename=payload.confirm_filename
            )
        )
    except AnnotationError as error:
        return _error(error)
