from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from literature_workbench.features.reader.pdf_stream import (
    ByteRange,
    DocumentIdentityChanged,
    DocumentNotFound,
    DocumentNotReadable,
    RangeNotSatisfiable,
    load_registered_pdf,
    open_verified_pdf,
    parse_single_range,
    stream_pdf,
)


router = APIRouter(prefix="/api/documents", tags=["reader"])


def _base_headers(etag: str) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }


def _etag_matches(candidate: str | None, etag: str) -> bool:
    if candidate is None:
        return False
    return any(
        item.strip() == "*" or item.strip().removeprefix("W/") == etag
        for item in candidate.split(",")
    )


@router.get("/{document_id}/file")
def get_pdf_file(request: Request, document_id: UUID) -> Response:
    if request.query_params:
        return Response(status_code=422)
    try:
        registered = load_registered_pdf(request.app.state.database, document_id)
    except DocumentNotFound:
        return JSONResponse({"code": "document_not_found"}, status_code=404)
    except DocumentNotReadable:
        return Response(status_code=409)
    headers = _base_headers(registered.etag)
    if _etag_matches(request.headers.get("If-None-Match"), registered.etag):
        return Response(status_code=304, headers=headers)
    try:
        selected = parse_single_range(request.headers.get("Range"), registered.size)
    except RangeNotSatisfiable:
        headers["Content-Range"] = f"bytes */{registered.size}"
        return Response(status_code=416, headers=headers)
    try:
        handle = open_verified_pdf(request.app.state.file_inspector, registered)
    except DocumentIdentityChanged:
        return Response(status_code=409)
    byte_range = selected or ByteRange(0, registered.size - 1)
    headers["Content-Length"] = str(byte_range.length)
    status_code = 206 if selected is not None else 200
    if selected is not None:
        headers["Content-Range"] = (
            f"bytes {byte_range.start}-{byte_range.end}/{registered.size}"
        )
    return StreamingResponse(
        stream_pdf(handle, byte_range),
        status_code=status_code,
        media_type="application/pdf",
        headers=headers,
    )
