from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from literature_workbench.api.workspace import archive_answer
from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.translation.repository import SelectionRejected, TranslationRepository
from literature_workbench.features.research.service import (
    LibraryAnswer,
    ResearchAnswer,
    ResearchError,
    ResearchService,
)

router = APIRouter(prefix="/api/documents", tags=["research"])
library_router = APIRouter(prefix="/api/libraries", tags=["research"])

# Codes raised by the service that map to a stable client-facing status.
_STATUS_BY_CODE = {
    "document_not_found": 404,
    "parse_required": 409,
    "question_empty": 400,
    "question_too_long": 400,
    "invalid_document_scope": 400,
    "selection_version_stale": 409,
    "full_text_not_ready": 409,
    "full_text_incomplete": 409,
    "full_text_empty_page": 409,
    "full_text_source_changed": 409,
    "full_text_too_large": 413,
    "full_text_online_required": 409,
    "full_text_scope_required": 400,
    "full_text_plan_changed": 409,
    "full_text_cache_unavailable": 503,
}


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_version_id: UUID
    content_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    physical_page: int = Field(ge=1)
    selected_text: str = Field(min_length=1, max_length=8_000)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    source_selection: SelectionInput | None = None


class LibraryAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2_000)
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=8)
    reading_mode: Literal["retrieval", "full_text"] = "retrieval"
    source_versions: dict[UUID, str] | None = Field(default=None, max_length=8)
    reuse_analysis: bool = True
    force_refresh: bool = False
    plan_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FullTextPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2_000)
    document_ids: list[UUID] = Field(min_length=1, max_length=8)
    reuse_analysis: bool = True
    force_refresh: bool = False


def _archive(request: Request, question: str, body: dict[str, object], scope: list[str]) -> dict[str, object]:
    try:
        body["entry_id"] = archive_answer(request.app.state.database, question, body, scope)
        body["history_saved"] = True
    except sqlite3.Error:
        body["history_saved"] = False
    return body


def _service(request: Request) -> ResearchService:
    return cast(ResearchService, request.app.state.research_service)


def _body(answer: ResearchAnswer) -> dict[str, object]:
    return {
        "answer": answer.answer,
        "citations": [
            {
                "physical_page": citation.physical_page,
                "excerpt": citation.excerpt,
                "rects": [list(rect) for rect in citation.rects],
            }
            for citation in answer.citations
        ],
        "provider": answer.provider,
        "model": answer.model,
        "fallback_reason": answer.fallback_reason,
        "citation_status": answer.citation_status,
    }


@router.post("/{document_id}/ask")
def ask_document(request: Request, document_id: UUID, payload: AskRequest) -> JSONResponse:
    try:
        if payload.source_selection is not None:
            selection = TranslationRepository(request.app.state.database, lambda: datetime.now(UTC)).verify_selection(
                document_id=document_id, **payload.source_selection.model_dump(), target_language="zh-CN",
            )
            answer = _service(request).ask(document_id=document_id, question=payload.question, selection=selection)
        else:
            answer = _service(request).ask(document_id=document_id, question=payload.question)
    except SelectionRejected as error:
        return JSONResponse({"code": str(error)}, status_code=409)
    except ResearchError as error:
        code = str(error) or "research_failed"
        status = _STATUS_BY_CODE.get(code)
        if status is None:
            # Provider/qwen failures: the question was fine, the upstream
            # was not — 502 mirrors how translation surfaces these codes.
            status = 502
        return JSONResponse({"code": code}, status_code=status)
    body = _body(answer)
    with request.app.state.database.connect() as connection:
        row = connection.execute("SELECT display_name FROM documents WHERE id=?", (str(document_id),)).fetchone()
    body["scope"] = "document"
    body["document_id"] = str(document_id)
    body["content_id"] = answer.content_id
    if payload.source_selection is not None:
        body["selection_context"] = {"physical_page": selection.physical_page, "excerpt": selection.selected_text,
                                     "content_id": selection.content_id, "document_id": str(document_id)}
    for citation in cast(list[dict[str, object]], body["citations"]):
        citation.update(document_id=str(document_id), display_name=row[0] if row else "原文献", content_id=answer.content_id)
    return JSONResponse(_archive(request, payload.question, body, [str(document_id)]))


def _library_body(answer: LibraryAnswer) -> dict[str, object]:
    return {
        "answer": answer.answer,
        "citations": [
            {
                "document_id": citation.document_id,
                "content_id": citation.content_id,
                "display_name": citation.display_name,
                "physical_page": citation.physical_page,
                "excerpt": citation.excerpt,
                "rects": [list(rect) for rect in citation.rects],
            }
            for citation in answer.citations
        ],
        "provider": answer.provider,
        "model": answer.model,
        "fallback_reason": answer.fallback_reason,
        "indexed_documents": answer.indexed_documents,
        "citation_status": answer.citation_status,
        "reading_mode": answer.reading_mode,
        "reading_coverage": answer.reading_coverage,
    }


@library_router.post("/full-text/prepare")
def prepare_full_text(request: Request, payload: FullTextPrepareRequest) -> JSONResponse:
    try:
        result = _service(request).prepare_full_text(question=payload.question,
            document_ids=list(dict.fromkeys(str(item) for item in payload.document_ids)),
            reuse_analysis=payload.reuse_analysis, force_refresh=payload.force_refresh)
    except (ResearchError, FullTextError) as error:
        return JSONResponse({"code": str(error)}, status_code=_STATUS_BY_CODE.get(str(error), 502))
    return JSONResponse(result)


@library_router.get("/full-text/studies")
def study_summaries(request: Request) -> JSONResponse:
    try:
        return JSONResponse(_service(request).study_summaries())
    except FullTextError as error:
        return JSONResponse({"code": str(error)}, status_code=_STATUS_BY_CODE.get(str(error), 502))
    except sqlite3.Error:
        return JSONResponse({"code": "full_text_cache_unavailable"}, status_code=503)


@library_router.get("/full-text/studies/{document_id}")
def study_history(request: Request, document_id: UUID, offset: int = Query(default=0, ge=0)) -> JSONResponse:
    try:
        return JSONResponse(_service(request).study_history(str(document_id), offset))
    except FullTextError as error:
        return JSONResponse({"code": str(error)}, status_code=_STATUS_BY_CODE.get(str(error), 502))
    except sqlite3.Error:
        return JSONResponse({"code": "full_text_cache_unavailable"}, status_code=503)


@library_router.post("/ask")
def ask_library(request: Request, payload: LibraryAskRequest) -> JSONResponse:
    try:
        selected = list(dict.fromkeys(str(item) for item in payload.document_ids)) if payload.document_ids is not None else None
        if payload.reading_mode == "full_text":
            if selected is None:
                raise FullTextError("full_text_scope_required")
            if payload.source_versions is None:
                raise FullTextError("full_text_source_changed")
            if payload.plan_id is None:
                raise FullTextError("full_text_plan_changed")
            answer = _service(request).ask_full_text(question=payload.question, document_ids=selected,
                source_versions={str(key): value for key, value in payload.source_versions.items()},
                reuse_analysis=payload.reuse_analysis, force_refresh=payload.force_refresh, plan_id=payload.plan_id)
        else:
            answer = _service(request).ask_library(question=payload.question) if selected is None else _service(request).ask_library(question=payload.question, document_ids=selected)
    except (ResearchError, FullTextError) as error:
        code = str(error) or "research_failed"
        status = _STATUS_BY_CODE.get(code)
        if status is None:
            status = 502
        return JSONResponse({"code": code}, status_code=status)
    body = _library_body(answer)
    body["scope"] = "selected" if selected is not None else "library"
    body["selected_count"] = len(selected) if selected is not None else None
    body["cited_documents"] = len({item.document_id for item in answer.citations})
    if selected is not None:
        with request.app.state.database.connect() as connection:
            names = {str(row["id"]): str(row["display_name"]) for row in connection.execute(
                "SELECT id,display_name FROM documents WHERE id IN (" + ",".join("?" for _ in selected) + ")", selected
            )}
        cited = {item.document_id for item in answer.citations}
        body["scope_documents"] = [{"document_id": item, "display_name": names.get(item, "原文献"), "cited": item in cited} for item in selected]
    return JSONResponse(_archive(request, payload.question, body, selected or []))
