from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/api/documents", tags=["documents"])


def _search_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _relative_folder(row: sqlite3.Row) -> str:
    try:
        relative = Path(str(row["path"])).relative_to(Path(str(row["root_path"])))
    except ValueError:
        return ""
    parent = relative.parent.as_posix()
    return "" if parent == "." else parent


def _authors(value: object) -> list[str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        return None
    return parsed


def _serialize(row: sqlite3.Row) -> dict[str, Any]:
    content_id = None if row["content_sha256"] is None else str(row["content_sha256"])
    return {
        "document_id": str(row["id"]),
        "current_document_version_id": row["current_document_version_id"],
        "content_id": content_id,
        "etag": content_id,
        "filename": str(row["display_name"]),
        "title": row["title"],
        "authors": _authors(row["authors_json"]),
        "relative_folder": _relative_folder(row),
        "size_bytes": int(row["size_bytes"]),
        "status": str(row["status"]),
        "duplicate_count": int(row["duplicate_count"]),
        "parsing_state": row["content_status"] or "not_applicable",
        "cloud_policy": {
            "auto_cloud_eligible": bool(row["auto_cloud_eligible"]),
            "authorization_source": str(row["authorization_source"]),
        },
    }


_SELECT = """
SELECT d.*, l.root_path, c.title, c.authors_json, c.status AS content_status,
  (
    SELECT dv.id FROM document_versions AS dv
    WHERE dv.document_id = d.id
    ORDER BY dv.observed_at DESC, dv.rowid DESC LIMIT 1
  ) AS current_document_version_id,
  CASE WHEN d.content_sha256 IS NULL THEN 0 ELSE (
    SELECT COUNT(*) FROM documents AS duplicate
    WHERE duplicate.content_sha256 = d.content_sha256 AND duplicate.status <> 'missing'
  ) END AS duplicate_count
FROM documents AS d
JOIN libraries AS l ON l.id = d.library_id
LEFT JOIN contents AS c ON c.sha256 = d.content_sha256
"""


@router.get("")
def list_documents(
    request: Request,
    q: str = "",
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Response:
    clauses: list[str] = []
    parameters: list[object] = []
    if q:
        pattern = _search_pattern(q)
        clauses.append(
            "(d.display_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR c.title LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR c.authors_json LIKE ? ESCAPE '\\' COLLATE NOCASE)"
        )
        parameters.extend((pattern, pattern, pattern))
    if status is not None:
        clauses.append("d.status = ?")
        parameters.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with request.app.state.database.connect() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM documents AS d "
            "LEFT JOIN contents AS c ON c.sha256 = d.content_sha256" + where,
            parameters,
        ).fetchone()[0]
        rows = connection.execute(
            _SELECT + where + " ORDER BY d.display_name COLLATE NOCASE, d.id LIMIT ? OFFSET ?",
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
    return JSONResponse(
        {"items": [_serialize(row) for row in rows], "total": int(total), "page": page,
         "page_size": page_size}
    )


@router.get("/{document_id}")
def get_document(request: Request, document_id: UUID) -> Response:
    with request.app.state.database.connect() as connection:
        row = connection.execute(_SELECT + " WHERE d.id = ?", (str(document_id),)).fetchone()
    if row is None:
        return JSONResponse({"code": "document_not_found"}, status_code=404)
    response = JSONResponse(_serialize(row))
    if row["content_sha256"] is not None:
        response.headers["ETag"] = f'"{row["content_sha256"]}"'
    return response
