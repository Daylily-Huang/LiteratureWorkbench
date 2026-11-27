from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zipfile
from pathlib import Path
from threading import Event
from uuid import uuid4

import fitz
import pytest

from literature_workbench.features.backups import BackupError, BackupService, file_hash


def headers(authenticated):
    return {'Origin': 'http://testserver', 'X-LW-CSRF': authenticated.csrf}


def finish(client, response, *, completed=True):
    assert response.status_code == 202, response.text
    identifier = response.json()['id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        value = client.get(f'/api/data/jobs/{identifier}').json()
        if value['status'] in {'completed', 'failed'}:
            assert value['status'] == ('completed' if completed else 'failed'), value
            return value
        time.sleep(.01)
    raise AssertionError('Backup did not finish in test deadline')


def seed_pdf(client, tmp_path):
    library = tmp_path/'source-library'
    library.mkdir()
    path = library/'paper.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 80), 'Original evidence for the research backup.')
        pdf.save(path)
    inspector = client.app.state.file_inspector
    registered = client.app.state.library_repository.register(inspector.inspect_root_directory(library), recursive=True, authorization_source='test')
    client.app.state.library_scanner.scan_library(registered.id)
    return path, client.get('/api/documents').json()['items'][0]['document_id']


def test_full_backup_preserves_database_pdfs_annotation_recovery_and_restores_only_copy(client, authenticated, tmp_path):
    auth = headers(authenticated)
    source, doc = seed_pdf(client, tmp_path)
    detail = client.get(f'/api/documents/{doc}').json()
    response = client.post(f'/api/documents/{doc}/annotations', headers=auth, json={
        'document_version_id': detail['current_document_version_id'], 'content_id': detail['content_id'],
        'page': 1, 'type': 'highlight', 'quads': [[50, 750, 300, 750, 50, 775, 300, 775]], 'color': 'yellow', 'comment': 'Research note',
    })
    assert response.status_code == 201, response.text
    note = client.post('/api/workspace/notebook', headers=auth, json={'title': 'My finding', 'note': 'A saved personal thought.'})
    assert note.status_code == 201
    root = client.app.state.settings.data_root
    runtime = root/'runtime'
    runtime.mkdir(exist_ok=True)
    (runtime/'private-session.json').write_text('private-session-sentinel', encoding='utf-8')
    original_hash = file_hash(source)
    created = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': True}))
    assert created['pdf_count'] == 1 and created['verification_status'] == 'passed'
    archive = client.app.state.backup_service.download(created['id'])
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['documents'][0]['sha256'] == original_hash
        assert manifest['counts']['research_entries'] == 1
        assert any(f['path'].startswith('data/.annotation-backups/') for f in manifest['files'])
        assert not any('/runtime/' in name for name in z.namelist())
        assert all(b'private-session-sentinel' not in z.read(name) for name in z.namelist())
    verify = finish(client, client.post(f'/api/data/backups/{created["id"]}/verify', headers=auth))
    assert verify['verified_files'] == created['file_count']
    # Changes made after the snapshot must survive a recovery rehearsal.
    with client.app.state.database.transaction() as c:
        c.execute("UPDATE research_entries SET note='A newer thought.'")
    source.write_bytes(source.read_bytes() + b'\n%later-source-change')
    changed_hash = file_hash(source)
    restored = finish(client, client.post(f'/api/data/backups/{created["id"]}/restore-preview', headers=auth))
    candidate = Path(restored['restore_preview'])
    assert candidate.is_relative_to(root/'backups/workspace/restore-previews')
    with sqlite3.connect(candidate/'data/db/literature-workbench.sqlite3') as c:
        assert c.execute('SELECT note FROM research_entries').fetchone()[0] == 'A saved personal thought.'
    with client.app.state.database.connect() as c:
        assert c.execute('SELECT note FROM research_entries').fetchone()[0] == 'A newer thought.'
    assert file_hash(source) == changed_hash
    assert file_hash(candidate/manifest['documents'][0]['archive_path']) == original_hash
    downloaded = client.get(f'/api/data/backups/{created["id"]}/download')
    assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == created['archive_sha256']


def test_database_only_does_not_claim_original_pdf_coverage_and_retains_committed_wal(client, authenticated, tmp_path):
    source, _ = seed_pdf(client, tmp_path)
    with client.app.state.database.transaction() as c:
        c.execute("INSERT INTO research_entries(id,title,question,note,answer_json,scope_json,created_at,updated_at) VALUES('n','Title','','Committed WAL note','{}','[]','now','now')")
    source.unlink()
    value = finish(client, client.post('/api/data/backups', headers=headers(authenticated), json={'include_pdfs': False}))
    assert value['pdf_count'] == 0
    with zipfile.ZipFile(client.app.state.backup_service.download(value['id'])) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert not manifest['include_pdfs'] and not manifest['documents']
        assert z.namelist() == ['data/db/literature-workbench.sqlite3', 'manifest.json']
        assert manifest['counts']['research_entries'] == 1


def test_unreadable_pdf_fails_full_backup_instead_of_silently_skipping(client, authenticated, tmp_path):
    source, _ = seed_pdf(client, tmp_path)
    source.unlink()
    failed = finish(client, client.post('/api/data/backups', headers=headers(authenticated), json={'include_pdfs': True}), completed=False)
    assert failed['error'] == 'backup_source_changed'
    assert not list(client.app.state.backup_service.root.glob('*.lwb.zip'))
    assert not list(client.app.state.backup_service.root.glob('*.part'))


def test_corruption_is_detected_without_changing_live_records(client, authenticated):
    auth = headers(authenticated)
    value = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': False}))
    service = client.app.state.backup_service
    path = service.download(value['id'])
    with path.open('ab') as out:
        out.write(b'tampered')
    failed = finish(client, client.post(f'/api/data/backups/{value["id"]}/verify', headers=auth), completed=False)
    assert failed['error'] == 'backup_hash_mismatch'
    assert service.job(value['id'])['verification_status'] == 'failed'
    assert client.get('/api/data/overview').json()['counts']['research_entries'] == 0


def test_missing_annotation_recovery_file_does_not_pass_as_full_backup(client, authenticated, tmp_path):
    source, doc = seed_pdf(client, tmp_path)
    root = client.app.state.settings.data_root
    missing = root/'.annotation-backups'/doc/'baseline.pdf'
    with client.app.state.database.transaction() as c:
        c.execute('INSERT INTO pdf_annotation_baselines VALUES(?,?,?,?)', (doc, file_hash(source), str(missing), 'now'))
    failed = finish(client, client.post('/api/data/backups', headers=headers(authenticated), json={'include_pdfs': True}), completed=False)
    assert failed['error'] == 'backup_recovery_missing'
    assert not list(client.app.state.backup_service.root.glob('*.lwb.zip'))


@pytest.mark.parametrize('name', ['data/../../escape', 'data/.. /escape', 'data/C:/escape', 'data/foo//bar', 'data/NUL.txt', 'data/file:stream'])
def test_recovery_rejects_unsafe_zip_paths_before_any_outside_write(client, authenticated, name):
    service = client.app.state.backup_service
    identifier = str(uuid4())
    path = service.root/f'{identifier}.lwb.zip'
    with zipfile.ZipFile(path, 'x') as z:
        z.writestr(name, b'evil')
        z.writestr('manifest.json', json.dumps({'format': 'literature-workbench-backup', 'version': 1,
            'files': [{'path': name, 'size': 4, 'sha256': hashlib.sha256(b'evil').hexdigest()}]}))
    service._update(identifier, kind='create', status='completed', archive_sha256=file_hash(path))
    failed = finish(client, client.post(f'/api/data/backups/{identifier}/restore-preview', headers=headers(authenticated)), completed=False)
    assert failed['error'] == 'backup_manifest_invalid'
    assert not list((service.root/'restore-previews').iterdir())
    assert not (service.root/'escape').exists()


def test_duplicate_submission_is_bounded_and_restart_marks_interrupted_operation(client, authenticated, monkeypatch):
    service = client.app.state.backup_service
    original = service._create
    entered, release = Event(), Event()
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(service, '_create', delayed)
    response = client.post('/api/data/backups', headers=headers(authenticated), json={'include_pdfs': False})
    assert entered.wait(5)
    try:
        duplicate = client.post('/api/data/backups', headers=headers(authenticated), json={'include_pdfs': False})
        assert duplicate.status_code == 409 and duplicate.json()['code'] == 'backup_busy'
    finally:
        release.set()
    finish(client, response)
    interrupted = str(uuid4())
    service._update(interrupted, kind='create', status='running')
    restarted = BackupService(service.database, service.data_root, service.inspector)
    try:
        assert restarted.job(interrupted)['status'] == 'failed'
        assert restarted.job(interrupted)['error'] == 'backup_interrupted'
    finally:
        restarted.close()


def test_data_api_has_session_csrf_and_bounded_inputs(client, authenticated):
    assert client.post('/api/data/backups', headers={'Origin': 'http://testserver'}, json={}).status_code == 403
    auth = headers(authenticated)
    assert client.post('/api/data/backups', headers=auth, json={'path': 'D:/arbitrary'}).status_code == 422
    assert client.post('/api/data/backups', headers=auth, json={'include_pdfs': 'false'}).status_code == 422
    assert client.post(f'/api/data/backups/{uuid4()}/verify', headers=auth).status_code == 404
    client.cookies.clear()
    assert client.get('/api/data/overview').status_code == 403


@pytest.mark.parametrize('problem', [BackupError('backup_unsafe_path'), OSError('storage denied')])
def test_unavailable_backup_storage_does_not_prevent_reader_startup(tmp_path, secret_store, monkeypatch, problem):
    from fastapi.testclient import TestClient
    import literature_workbench.api.app as app_module
    from literature_workbench.config import AppSettings
    from literature_workbench.security.local_session import LocalSession
    def unavailable(*args):
        raise problem
    monkeypatch.setattr(app_module, 'BackupService', unavailable)
    session = LocalSession()
    app = app_module.create_app(AppSettings(data_root=tmp_path/'data', allowed_hosts=('testserver',)),
        local_session=session, secret_store=secret_store, register_initial_library=False)
    with TestClient(app, base_url='http://testserver', cookies={'lw_session': session.session_cookie}) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/documents').status_code == 200
        result = client.get('/api/data/overview')
        assert result.status_code == 503 and result.json()['code'] == 'backup_storage_unavailable'
