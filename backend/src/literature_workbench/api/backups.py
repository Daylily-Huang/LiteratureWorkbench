from __future__ import annotations

from typing import cast
import asyncio
import hashlib
import os
import anyio
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from literature_workbench.features.backups import BackupError, BackupService, CHUNK, io_path

router = APIRouter(prefix='/api/data', tags=['local-data'])


class BackupInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    include_pdfs: bool = True


def service(request: Request) -> BackupService:
    result = cast(BackupService | None, request.app.state.backup_service)
    if result is None:
        raise BackupError('backup_storage_unavailable')
    return result


def failure(error: BackupError) -> JSONResponse:
    code = str(error)
    status = {'backup_not_found':404, 'backup_busy':409, 'backup_file_type_invalid':422,
        'backup_too_large':413, 'backup_upload_incomplete':400, 'backup_schema_unsupported':409,
        'backup_cancelled':409}.get(code, 503)
    return JSONResponse({'code': code}, status_code=status)


@router.get('/overview')
def overview(request: Request) -> JSONResponse:
    try:
        return JSONResponse(service(request).overview(), headers={'Cache-Control': 'no-store'})
    except BackupError as error:
        return failure(error)


@router.get('/context')
def context(request: Request) -> dict[str, object]:
    root = request.app.state.settings.data_root
    return {'recovered': (root.parent/'recovered-workspace.json').is_file(), 'data_root': str(root),
            'launch_address': getattr(request.app.state, 'launch_address', None)}


@router.post('/backups')
def create(request: Request, payload: BackupInput) -> JSONResponse:
    try:
        return JSONResponse(service(request).start(include_pdfs=payload.include_pdfs), status_code=202)
    except BackupError as error:
        return failure(error)


@router.get('/jobs/{operation_id}')
def job(request: Request, operation_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(service(request).job(str(operation_id)), headers={'Cache-Control': 'no-store'})
    except BackupError as error:
        return failure(error)


@router.post('/jobs/{operation_id}/cancel')
def cancel_job(request: Request, operation_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(service(request).cancel(str(operation_id)))
    except BackupError as error:
        return failure(error)


@router.post('/backups/import')
async def import_backup(request: Request) -> JSONResponse:
    if request.headers.get('Content-Type') != 'application/octet-stream':
        return JSONResponse({'code':'backup_file_type_invalid'}, status_code=415)
    length = request.headers.get('Content-Length', '')
    if not length.isascii() or not length.isdigit():
        return JSONResponse({'code':'backup_upload_incomplete'}, status_code=411)
    try:
        name = unquote(request.headers.get('X-LW-Backup-Filename', ''), errors='strict')
        manager = service(request)
        job = manager.begin_import(name, int(length))
    except (ValueError, UnicodeError):
        return failure(BackupError('backup_file_type_invalid'))
    except BackupError as error:
        return failure(error)
    operation = job['id']
    failure_code = 'backup_io_error'
    completed = False
    try:
        size = reported = 0
        digest = hashlib.sha256()
        iterator = request.stream().__aiter__()
        with io_path(manager.import_path(operation)).open('xb') as output:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(iterator), timeout=30)
                except StopAsyncIteration:
                    break
                manager._cancelled()
                size += len(chunk)
                if size > job['expected_bytes']:
                    raise BackupError('backup_upload_incomplete')
                digest.update(chunk)
                await run_in_threadpool(output.write, chunk)
                if size - reported >= CHUNK:
                    await run_in_threadpool(manager._update, operation, received_bytes=size)
                    reported = size
            if size != job['expected_bytes']:
                raise BackupError('backup_upload_incomplete')
            await run_in_threadpool(output.flush)
            await run_in_threadpool(os.fsync, output.fileno())
        result = manager.finish_import(operation, digest.hexdigest(), size)
        completed = True
        return JSONResponse(result, status_code=202)
    except BackupError as error:
        failure_code = str(error)
    except (ClientDisconnect, asyncio.TimeoutError, asyncio.CancelledError):
        failure_code = 'backup_upload_incomplete'
    except OSError:
        failure_code = 'backup_io_error'
    finally:
        if not completed:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(manager.fail_import, operation, failure_code)
    return failure(BackupError(failure_code))


@router.post('/backups/{backup_id}/verify')
def verify(request: Request, backup_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(service(request).start(kind='verify', backup_id=str(backup_id)), status_code=202)
    except BackupError as error:
        return failure(error)


@router.post('/backups/{backup_id}/restore-preview')
def restore_preview(request: Request, backup_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(service(request).start(kind='restore_preview', backup_id=str(backup_id)), status_code=202)
    except BackupError as error:
        return failure(error)


@router.get('/backups/{backup_id}/download', response_model=None)
def download(request: Request, backup_id: UUID) -> FileResponse | JSONResponse:
    try:
        path = service(request).download(str(backup_id))
        return FileResponse(path, media_type='application/zip', filename=f'文献工作台备份-{backup_id}.lwb.zip',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    except BackupError as error:
        return failure(error)


@router.post('/backups/{backup_id}/recover-workspace')
def recover_workspace(request: Request, backup_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(service(request).start(kind='recover_workspace', backup_id=str(backup_id)), status_code=202)
    except BackupError as error:
        return failure(error)


@router.post('/recoveries/{operation_id}/open')
def open_workspace(request: Request, operation_id: UUID) -> JSONResponse:
    try:
        from literature_workbench.launcher import open_recovered_workspace
        workspace = service(request).recovery_workspace(str(operation_id))
        open_recovered_workspace(workspace)
        return JSONResponse({'requested': True, 'workspace_root': str(workspace)}, status_code=202)
    except BackupError as error:
        return failure(error)
    except OSError:
        return failure(BackupError('recovery_launch_failed'))


@router.post('/recoveries/{operation_id}/stop')
def stop_workspace(request: Request, operation_id: UUID) -> JSONResponse:
    try:
        from literature_workbench.launcher import stop_running_instance
        workspace = service(request).recovery_workspace(str(operation_id))
        stopped = stop_running_instance(workspace/'runtime/launcher/launcher.json')
        return JSONResponse({'stopped': stopped})
    except BackupError as error:
        return failure(error)
    except (OSError, RuntimeError):
        return failure(BackupError('recovery_stop_failed'))
