from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from literature_workbench.features.library.roots import (
    LibraryRootService,
    SelectionIdentityChanged,
)
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.folder_dialog import FolderDialogError
from literature_workbench.platform.windows.filesystem import SafeFileInspector, UnsafePath
from literature_workbench.security.selection_tokens import (
    InvalidSelectionToken,
    RootSelectionTokens,
)


router = APIRouter(prefix="/api/libraries", tags=["libraries"])


class NativeFolderDialog(Protocol):
    def select(self) -> Path | None: ...


class RegisterLibraryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_token: str
    recursive: bool = True


def _dialog(request: Request) -> NativeFolderDialog:
    return cast(NativeFolderDialog, request.app.state.folder_dialog)


def _inspector(request: Request) -> SafeFileInspector:
    return cast(SafeFileInspector, request.app.state.file_inspector)


def _tokens(request: Request) -> RootSelectionTokens:
    return cast(RootSelectionTokens, request.app.state.selection_tokens)


def _root_service(request: Request) -> LibraryRootService:
    return cast(LibraryRootService, request.app.state.library_root_service)


def _repository(request: Request) -> LibraryRepository:
    return cast(LibraryRepository, request.app.state.library_repository)


def _queue(request: Request) -> TaskQueue:
    return cast(TaskQueue, request.app.state.task_queue)


def _session_generation(request: Request) -> UUID:
    return UUID(request.app.state.local_session.session_generation)


@router.get("")
def list_libraries(request: Request) -> Response:
    with request.app.state.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT l.id,l.root_path,l.recursive,
              (SELECT COUNT(*) FROM documents d WHERE d.library_id=l.id
                AND d.format='pdf' AND d.status IN ('candidate_pdf','valid_pdf')) AS document_count,
              (SELECT j.status FROM jobs j WHERE j.dedup_key='scan_library:' || l.id
                ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1) AS scan_state,
              s.value_json AS scan_summary,s.updated_at AS last_synced_at
            FROM libraries l LEFT JOIN app_settings s ON s.key='library_scan_status:' || l.id
            WHERE l.status='active' AND l.discovery_mode='monitored'
            ORDER BY l.created_at,l.id
            """
        ).fetchall()
    items = []
    for row in rows:
        summary = json.loads(row["scan_summary"]) if row["scan_summary"] else {}
        items.append({
            "library_id": str(row["id"]),
            "display_path": str(row["root_path"]),
            "recursive": bool(row["recursive"]),
            "document_count": int(row["document_count"]),
            "scan_state": row["scan_state"] or "idle",
            "last_synced_at": row["last_synced_at"],
            "warnings": summary.get("warnings", []),
        })
    return JSONResponse({"items": items})


@router.post("/select-root")
def select_root(request: Request) -> Response:
    try:
        selected = _dialog(request).select()
    except FolderDialogError:
        return JSONResponse({"code": "folder_dialog_failed"}, status_code=503)
    if selected is None:
        return Response(status_code=204)
    try:
        identity = _inspector(request).inspect_root_directory(selected)
    except (OSError, UnsafePath):
        return JSONResponse({"code": "unsafe_library_root"}, status_code=409)
    token = _tokens(request).issue(identity, _session_generation(request), ttl_seconds=300)
    return JSONResponse(
        {
            "selection_token": token,
            "display_path": str(identity.canonical_path),
            "expires_in_seconds": 300,
        }
    )


@router.post("", status_code=201)
async def register_library(request: Request, payload: RegisterLibraryRequest) -> Response:
    try:
        selected = _tokens(request).consume(
            payload.selection_token,
            _session_generation(request),
        )
    except InvalidSelectionToken:
        return JSONResponse({"code": "invalid_selection_token"}, status_code=403)
    try:
        library = _root_service(request).register_selected_root(
            selected,
            recursive=payload.recursive,
        )
    except (SelectionIdentityChanged, UnsafePath, OSError):
        return JSONResponse({"code": "selection_identity_changed"}, status_code=409)
    except ValueError as error:
        code = str(error)
        if code in {
            "overlapping_library_root",
            "private_data_root_not_allowed",
            "registered_root_identity_changed",
            "library_recursive_mismatch",
        }:
            return JSONResponse({"code": code}, status_code=409)
        raise
    await request.app.state.library_watcher.add_library(library.id)
    return JSONResponse({"library_id": str(library.id)}, status_code=201)


@router.get("/{library_id}/scan-status")
def scan_status(request: Request, library_id: UUID) -> Response:
    try:
        _repository(request).get(library_id)
    except KeyError:
        return JSONResponse({"code": "library_not_found"}, status_code=404)
    with request.app.state.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM jobs WHERE dedup_key = ? GROUP BY status
            """,
            (f"scan_library:{library_id}",),
        ).fetchall()
        last_scan_row = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            (f"library_scan_status:{library_id}",),
        ).fetchone()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    last_scan = None
    if last_scan_row is not None:
        parsed = json.loads(str(last_scan_row["value_json"]))
        if isinstance(parsed, dict):
            last_scan = parsed
    return JSONResponse(
        {
            "library_id": str(library_id),
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "last_scan": last_scan,
        }
    )


@router.post("/{library_id}/scan", status_code=202)
def request_scan(request: Request, library_id: UUID) -> Response:
    try:
        _repository(request).get(library_id)
    except KeyError:
        return JSONResponse({"code": "library_not_found"}, status_code=404)
    job_id = _queue(request).enqueue(
        "scan_library",
        f"scan_library:{library_id}",
        {"library_id": str(library_id)},
    )
    return JSONResponse({"job_id": str(job_id)}, status_code=202)
