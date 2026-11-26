from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.features.backups import BackupService
from literature_workbench.security.local_session import LocalSession
from test_backups import finish, headers, seed_pdf


def archive_bytes(client, authenticated, *, full=False):
    created = finish(client, client.post('/api/data/backups', headers=headers(authenticated), json={'include_pdfs':full}))
    return created, client.get(f'/api/data/backups/{created["id"]}/download').content


def upload(client, auth, body, name='Exported.lwb.zip', **extra):
    return client.post('/api/data/backups/import', headers={**auth, 'Content-Type':'application/octet-stream', 'X-LW-Backup-Filename':name, **extra}, content=body)


def rewrite(body, change):
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        files = {name:z.read(name) for name in z.namelist()}
    change(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def update_record(files, name, data):
    files[name] = data
    manifest = json.loads(files['manifest.json'])
    record = next(item for item in manifest['files'] if item['path'] == name)
    record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    files['manifest.json'] = json.dumps(manifest).encode()


def test_same_backup_is_verified_but_not_saved_twice_and_no_recovery_runs(client, authenticated):
    created, body = archive_bytes(client, authenticated)
    imported = finish(client, upload(client, headers(authenticated), body))
    assert imported['kind'] == 'import' and imported['duplicate_of'] == created['id']
    assert not imported['include_pdfs'] and imported['pdf_count'] == 0
    overview = client.get('/api/data/overview').json()
    assert overview['backups_total'] == 1 and not overview['recoveries']
    assert client.get(f'/api/data/backups/{imported["id"]}/download').content == body
    assert len(list(client.app.state.backup_service.root.glob('*.lwb.zip'))) == 1
    assert not list(client.app.state.backup_service.root.glob('*.part'))


def test_external_complete_backup_can_move_to_new_workspace_without_old_folder(client, authenticated, tmp_path, secret_store):
    source, doc = seed_pdf(client, tmp_path)
    client.post('/api/workspace/notebook', headers=headers(authenticated), json={'title':'Migration thought','note':'A saved decision'})
    _, body = archive_bytes(client, authenticated, full=True)
    source.parent.rename(tmp_path/'offline-library')
    session = LocalSession()
    with TestClient(create_app(AppSettings(data_root=tmp_path/'new-machine'), local_session=session, secret_store=secret_store, register_initial_library=False)) as target:
        target.post('/api/session/bootstrap', headers={'Origin':'http://127.0.0.1'}, json={'token':session.issue_bootstrap_token()})
        auth = {'Origin':'http://127.0.0.1','X-LW-CSRF':session.csrf_token}
        imported = finish(target, upload(target, auth, body))
        assert imported['include_pdfs'] and imported['pdf_count'] == 1 and not imported.get('duplicate_of')
        assert target.get('/api/documents').json()['items'] == []
        assert target.get('/api/data/overview').json()['recoveries'] == []
        restored = finish(target, target.post(f'/api/data/backups/{imported["id"]}/recover-workspace', headers=auth))
        root = Path(restored['workspace_root'])
        with sqlite3.connect(root/'db/literature-workbench.sqlite3') as c:
            assert c.execute('SELECT note FROM research_entries').fetchone()[0] == 'A saved decision'
            row = c.execute('SELECT id,path FROM documents').fetchone()
            assert row[0] == doc and Path(row[1]).is_relative_to(root)
        assert target.get('/api/workspace/notebook').json()['items'] == []


@pytest.mark.parametrize('mode', ['extra-trigger','extra-version'])
def test_unrecognized_sqlite_schema_or_migration_is_rejected_even_when_all_hashes_match(client, authenticated, tmp_path, mode):
    _, body = archive_bytes(client, authenticated)
    def alter(files):
        path = tmp_path/'malicious.sqlite3'
        path.write_bytes(files['data/db/literature-workbench.sqlite3'])
        with sqlite3.connect(path) as c:
            c.execute('PRAGMA journal_mode=DELETE')
            if mode == 'extra-trigger':
                c.execute("CREATE TRIGGER bad_job AFTER UPDATE ON jobs BEGIN INSERT INTO app_settings VALUES('trap','true',0,'now'); END")
            else:
                c.execute("INSERT INTO schema_migrations VALUES('9999_unsupported','now')")
        update_record(files, 'data/db/literature-workbench.sqlite3', path.read_bytes())
    poisoned = rewrite(body, alter)
    failed = finish(client, upload(client, headers(authenticated), poisoned), completed=False)
    assert failed['error'] == 'backup_schema_unsupported'
    assert not list(client.app.state.backup_service.root.glob('*.part'))
    with client.app.state.database.connect() as c:
        assert not c.execute("SELECT 1 FROM app_settings WHERE key='trap'").fetchone()


def test_zip_runtime_payload_and_case_collisions_are_rejected(client, authenticated):
    _, body = archive_bytes(client, authenticated)
    for path in ['data/runtime/launcher/worker-bootstrap.json','data/DB/literature-workbench.sqlite3']:
        def alter(files):
            files[path] = b'forged capability'
            m = json.loads(files['manifest.json'])
            m['files'].append({'path':path,'size':len(files[path]),'sha256':hashlib.sha256(files[path]).hexdigest()})
            files['manifest.json'] = json.dumps(m).encode()
        failed = finish(client, upload(client, headers(authenticated), rewrite(body, alter)), completed=False)
        assert failed['error'] == 'backup_manifest_invalid'


def test_cancel_incoming_or_queued_import_does_not_cancel_another_job_and_releases_reservation(client, authenticated, monkeypatch):
    manager = client.app.state.backup_service
    first = manager.begin_import('Partial.lwb.zip', 20)
    assert client.post('/api/data/backups', headers=headers(authenticated), json={}).status_code == 409
    assert client.post(f'/api/data/jobs/{first["id"]}/cancel').status_code == 403
    assert client.post(f'/api/data/jobs/{first["id"]}/cancel', headers=headers(authenticated)).status_code == 200
    assert manager._cancel.is_set()
    manager.fail_import(first['id'], 'backup_cancelled')
    second = manager.begin_import('Next.lwb.zip', 20)
    manager.cancel(first['id'])
    assert not manager._cancel.is_set()
    manager.fail_import(second['id'], 'backup_upload_incomplete')
    created, _ = archive_bytes(client, authenticated)
    assert created['status'] == 'completed'


def test_partial_upload_is_not_retained_and_restart_marks_upload_interrupted(client, authenticated):
    response = upload(client, headers(authenticated), b'short', **{'Content-Length':'100'})
    assert response.status_code == 400 and response.json()['code'] == 'backup_upload_incomplete'
    manager = client.app.state.backup_service
    assert not list(manager.root.glob('*.part'))
    pending = manager.begin_import('Interrupted.lwb.zip', 20)
    manager.import_path(pending['id']).write_bytes(b'partial')
    manager.close()
    restarted = BackupService(manager.database, manager.data_root, manager.inspector)
    assert restarted.job(pending['id'])['error'] == 'backup_interrupted'
    assert not list(restarted.root.glob('*.part'))
    restarted.close()


def test_zip_directory_entry_count_is_checked_before_zipfile_allocates_records(client, authenticated):
    _, body = archive_bytes(client, authenticated)
    data = bytearray(body)
    offset = data.rfind(b'PK\x05\x06')
    struct.pack_into('<HH', data, offset + 8, 1, 1)  # Actual directory has two records.
    failed = finish(client, upload(client, headers(authenticated), bytes(data)), completed=False)
    assert failed['error'] == 'backup_manifest_invalid'


@pytest.mark.parametrize('name', ['wrong.pdf', '../Export.lwb.zip', 'folder\\Export.lwb.zip'])
def test_upload_filename_cannot_select_a_server_path(client, authenticated, name):
    assert upload(client, headers(authenticated), b'not a zip', name).status_code == 422
    assert not list(client.app.state.backup_service.root.glob('*.part'))


def test_import_requires_csrf_before_receiving_bytes(client, authenticated):
    response = client.post('/api/data/backups/import', headers={'Origin':'http://testserver','Content-Type':'application/octet-stream','X-LW-Backup-Filename':'Backup.lwb.zip'}, content=b'ignored')
    assert response.status_code == 403
    assert not list(client.app.state.backup_service.root.glob('*.part'))


def test_corrupted_existing_copy_is_not_reused_or_left_marked_valid(client, authenticated):
    created, body = archive_bytes(client, authenticated)
    manager = client.app.state.backup_service
    manager.download(created['id']).write_bytes(b'damaged existing archive')
    imported = finish(client, upload(client, headers(authenticated), body))
    assert not imported.get('duplicate_of')
    assert client.get(f'/api/data/backups/{imported["id"]}/download').content == body
    assert manager.job(created['id'])['verification_status'] == 'failed'


def test_disk_write_failure_cleans_partial_upload_and_can_retry(client, authenticated, monkeypatch):
    from literature_workbench.api import backups as routes
    _, body = archive_bytes(client, authenticated)
    real = routes.run_in_threadpool
    failed = False
    async def fail_once(function, *args, **kwargs):
        nonlocal failed
        if not failed and getattr(function, '__name__', '') == 'write':
            failed = True
            raise OSError('injected disk full')
        return await real(function, *args, **kwargs)
    monkeypatch.setattr(routes, 'run_in_threadpool', fail_once)
    response = upload(client, headers(authenticated), body)
    assert response.status_code == 503 and response.json()['code'] == 'backup_io_error'
    assert client.app.state.backup_service._uploading is None
    assert not list(client.app.state.backup_service.root.glob('*.part'))
    assert finish(client, upload(client, headers(authenticated), body))['status'] == 'completed'
