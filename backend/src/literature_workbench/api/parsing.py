from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.staging import _registered_pdf_from_row
from literature_workbench.features.reader.pdf_stream import open_verified_pdf


router = APIRouter(prefix="/api/documents", tags=["parsing"])


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["auto", "pymupdf", "mineru"] = "auto"
    ocr_language: Literal["en", "ch"] | None = None


class ParsePreferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preference: Literal["parse", "read_only"]


@router.get("/{document_id}/parse-status")
def parse_status(request: Request, document_id: UUID) -> Response:
    with request.app.state.database.connect() as connection:
        document = connection.execute(
            """
            SELECT d.parse_preference,d.parse_strategy_override,d.ocr_language_override,
                   d.content_sha256,c.status AS content_status,c.page_count,
                   c.current_parse_run_id
            FROM documents AS d
            LEFT JOIN contents AS c ON c.sha256=d.content_sha256
            WHERE d.id=?
            """,
            (str(document_id),),
        ).fetchone()
        if document is None:
            return JSONResponse({"code": "document_not_found"}, status_code=404)
        latest = connection.execute(
            """
            SELECT id,parser_name,parser_version,status,error_code,created_at,
                   updated_at,completed_at
            FROM parse_runs WHERE content_sha256=? ORDER BY rowid DESC LIMIT 1
            """,
            (document["content_sha256"],),
        ).fetchone()
    latest_payload = None
    if latest is not None:
        error_code = None if latest["error_code"] is None else str(latest["error_code"])
        latest_payload = {
            "parse_run_id": str(latest["id"]),
            "parser": str(latest["parser_name"]),
            "parser_version": str(latest["parser_version"]),
            "status": str(latest["status"]),
            "error_code": error_code,
            "retryable": _parse_error_is_retryable(error_code),
            "created_at": str(latest["created_at"]),
            "updated_at": str(latest["updated_at"]),
            "completed_at": latest["completed_at"],
        }
    return JSONResponse(
        {
            "preference": str(document["parse_preference"]),
            "strategy_override": document["parse_strategy_override"],
            "ocr_language_override": document["ocr_language_override"],
            "content_status": document["content_status"] or "not_applicable",
            "page_count": document["page_count"],
            "current_parse_run_id": document["current_parse_run_id"],
            "latest_run": latest_payload,
        }
    )


@router.post("/{document_id}/parse", status_code=202)
def request_parse(
    request: Request,
    document_id: UUID,
    payload: ParseRequest,
) -> Response:
    with request.app.state.database.transaction() as connection:
        document = connection.execute(
            """
            SELECT d.*,l.root_path,l.root_volume_id,l.root_file_id,l.recursive,
                   l.discovery_mode,l.authorization_source AS root_authorization_source,
                   l.status AS root_status
            FROM documents AS d JOIN libraries AS l ON l.id=d.library_id
            WHERE d.id=?
            """,
            (str(document_id),),
        ).fetchone()
        if document is None:
            return JSONResponse({"code": "document_not_found"}, status_code=404)
        if (
            document["content_sha256"] is None
            or str(document["format"]) != "pdf"
            or str(document["status"]) == "missing"
        ):
            return JSONResponse({"code": "document_not_parseable"}, status_code=409)
        try:
            current = open_verified_pdf(
                request.app.state.file_inspector,
                _registered_pdf_from_row(document),
            )
        except (OSError, ValueError):
            return JSONResponse({"code": "document_not_parseable"}, status_code=409)
        else:
            current.close()
        content_id = ContentId(str(document["content_sha256"]))
        connection.execute(
            """
            UPDATE documents
            SET parse_strategy_override=?,ocr_language_override=?
            WHERE id=?
            """,
            (payload.strategy, payload.ocr_language, str(document_id)),
        )
        job_id = request.app.state.task_queue.enqueue_in_transaction(
            connection,
            "parse_pdf",
            f"parse:{content_id}:{uuid4()}",
            {
                "document_id": str(document_id),
                "content_id": str(content_id),
                "strategy": payload.strategy,
                "ocr_language": payload.ocr_language,
            },
            "pending",
        )
    return JSONResponse({"job_id": str(job_id), "status": "queued"}, status_code=202)


@router.patch("/{document_id}/parse-preference")
def update_parse_preference(
    request: Request,
    document_id: UUID,
    payload: ParsePreferenceRequest,
) -> Response:
    with request.app.state.database.transaction() as connection:
        document = connection.execute(
            """
            SELECT content_sha256,parse_preference,parse_strategy_override,
                   ocr_language_override
            FROM documents WHERE id=?
            """,
            (str(document_id),),
        ).fetchone()
        if document is None:
            return JSONResponse({"code": "document_not_found"}, status_code=404)
        previous = str(document["parse_preference"])
        connection.execute(
            "UPDATE documents SET parse_preference=? WHERE id=?",
            (payload.preference, str(document_id)),
        )
        job_id = None
        if (
            previous == "read_only"
            and payload.preference == "parse"
            and document["content_sha256"] is not None
        ):
            content_id = ContentId(str(document["content_sha256"]))
            strategy = str(document["parse_strategy_override"] or "auto")
            language = document["ocr_language_override"]
            job_id = request.app.state.task_queue.enqueue_in_transaction(
                connection,
                "parse_pdf",
                f"parse:{content_id}:{uuid4()}",
                {
                    "document_id": str(document_id),
                    "content_id": str(content_id),
                    "strategy": strategy,
                    "ocr_language": language,
                },
                "pending",
            )
    body: dict[str, object] = {"preference": payload.preference}
    if job_id is not None:
        body.update({"job_id": str(job_id), "status": "queued"})
        return JSONResponse(body, status_code=202)
    return JSONResponse(body)


@router.get("/{document_id}/pages/{physical_page}/text")
def page_text(request: Request, document_id: UUID, physical_page: int) -> Response:
    if physical_page < 1:
        return JSONResponse({"code": "page_not_found"}, status_code=404)
    document_exists, page = (
        request.app.state.parse_run_repository.read_document_current_page(
            document_id, physical_page
        )
    )
    if not document_exists:
        return JSONResponse({"code": "document_not_found"}, status_code=404)
    if page is None:
        return JSONResponse({"code": "page_not_found"}, status_code=404)
    return JSONResponse(page.model_dump(mode="json"))


def _parse_error_is_retryable(error_code: str | None) -> bool | None:
    if error_code is None:
        return None
    return error_code not in {
        "invalid_pdf_structure",
        "mineru_invalid_input",
        "mineru_invalid_output",
        "mineru_page_count_mismatch",
    }
