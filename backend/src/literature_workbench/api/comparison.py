from __future__ import annotations

from typing import cast
import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.research.comparison import Comparison
from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.research.service import ResearchService

router = APIRouter(prefix="/api/workspace/comparisons", tags=["comparison"])


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_ids: list[UUID] = Field(min_length=2, max_length=8)
    question: str = Field(min_length=1, max_length=500)
    force_refresh: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def valid_scope(self) -> Scope:
        self.question = self.question.strip()
        if not self.question or len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("unique_papers_and_question_required")
        return self


class Analyze(Scope):
    request_id: UUID
    source_versions: dict[UUID, str]
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")


def _service(request: Request) -> Comparison:
    service = cast(ResearchService, request.app.state.research_service)
    return service.comparisons(request.app.state.local_session.session_generation)


def _error(error: Exception) -> JSONResponse:
    code = str(error)
    status = 404 if code == "comparison_not_found" else 413 if code == "full_text_too_large" else 502 if isinstance(error, EgressRejected) else 409
    return JSONResponse({"code": code}, status_code=status)


@router.get("")
def index(request: Request) -> JSONResponse:
    workspace = hashlib.sha256(str(request.app.state.settings.data_root.resolve()).casefold().encode()).hexdigest()
    with request.app.state.database.connect() as c:
        rows = c.execute("SELECT id,payload_json,created_at FROM jobs WHERE kind='structured_comparison' "
                         "AND stage<>'reference' ORDER BY created_at DESC,id DESC LIMIT 50").fetchall()
    return JSONResponse({"workspace_id": workspace, "items": [{"request_id": row[0],
        "document_ids": json.loads(row[1])["ids"], "question": json.loads(row[1])["question"],
        "created_at": row[2]} for row in rows]})


@router.post("/prepare")
def prepare(request: Request, payload: Scope) -> JSONResponse:
    try:
        return JSONResponse(_service(request).prepare([str(d) for d in payload.document_ids], payload.question, payload.force_refresh))
    except (FullTextError, EgressRejected) as error:
        return _error(error)


@router.get("/{request_id}")
def receipt(request: Request, request_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(_service(request).receipt(str(request_id)))
    except (FullTextError, EgressRejected) as error:
        return _error(error)


@router.post("")
def analyze(request: Request, payload: Analyze) -> JSONResponse:
    try:
        return JSONResponse(_service(request).analyze(str(payload.request_id), [str(d) for d in payload.document_ids],
            payload.question, payload.force_refresh, {str(k): v for k, v in payload.source_versions.items()}, payload.plan_id))
    except (FullTextError, EgressRejected) as error:
        return _error(error)
