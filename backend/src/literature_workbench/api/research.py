from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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
}


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)


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
    }


@router.post("/{document_id}/ask")
def ask_document(request: Request, document_id: UUID, payload: AskRequest) -> JSONResponse:
    try:
        answer = _service(request).ask(document_id=document_id, question=payload.question)
    except ResearchError as error:
        code = str(error) or "research_failed"
        status = _STATUS_BY_CODE.get(code)
        if status is None:
            # Provider/qwen failures: the question was fine, the upstream
            # was not — 502 mirrors how translation surfaces these codes.
            status = 502
        return JSONResponse({"code": code}, status_code=status)
    return JSONResponse(_body(answer))


def _library_body(answer: LibraryAnswer) -> dict[str, object]:
    return {
        "answer": answer.answer,
        "citations": [
            {
                "document_id": citation.document_id,
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
    }


@library_router.post("/ask")
def ask_library(request: Request, payload: AskRequest) -> JSONResponse:
    try:
        answer = _service(request).ask_library(question=payload.question)
    except ResearchError as error:
        code = str(error) or "research_failed"
        status = _STATUS_BY_CODE.get(code)
        if status is None:
            status = 502
        return JSONResponse({"code": code}, status_code=status)
    return JSONResponse(_library_body(answer))
