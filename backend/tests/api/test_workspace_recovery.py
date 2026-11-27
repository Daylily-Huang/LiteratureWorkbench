from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import fitz
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.features.backups import BackupError, file_hash, io_path
from literature_workbench.features.recovery import validate_workspace
from literature_workbench.security.local_session import LocalSession

from test_backups import finish, headers, seed_pdf


def test_recovered_workspace_reads_and_annotates_with_original_folder_offline(client, authenticated, tmp_path, secret_store):
    auth = headers(authenticated)
    source, document_id = seed_pdf(client, tmp_path)
    detail = client.get(f'/api/documents/{document_id}').json()
    payload = {'document_version_id': detail['current_document_version_id'], 'content_id': detail['content_id'],
        'page': 1, 'type': 'comment', 'quads': [[50, 750, 300, 750, 50, 775, 300, 775]], 'color': 'yellow', 'comment': 'Saved annotation'}
    assert client.post(f'/api/documents/{document_id}/annotations', headers=auth, json=payload).status_code == 201
    assert client.post('/api/workspace/notebook', headers=auth, json={'title': 'Research thought', 'note': 'Do not lose this.'}).status_code == 201
    with client.app.state.database.transaction() as c:
        c.execute('INSERT INTO translation_cache VALUES(?,?,?,?,?,?,?,?)', ('cached', '已缓存译文', 'en', 'zh', 'deepseek', 'test', 'v1', 'now'))
        c.execute('INSERT INTO reading_records(document_id,page,total_pages,last_opened) VALUES(?,?,?,?)', (document_id, 1, 1, 123))
        c.execute("INSERT INTO jobs(id,kind,dedup_key,payload_json,status,stage,created_at,updated_at) VALUES(?,'translate_selection','old-paid-work','{}','queued','queued','now','now')", (str(uuid4()),))
        parse_dir = client.app.state.settings.data_root/'parse-results/example-run'
        parse_dir.mkdir(parents=True)
        (parse_dir/'source.txt').write_text('Saved parser attachment', encoding='utf-8')
        run_id = str(uuid4())
        c.execute("INSERT INTO parse_runs(id,content_sha256,parser_name,parser_version,status,output_dir,created_at,updated_at) VALUES(?,?,'mineru','test','completed','parse-results/example-run','now','now')", (run_id, client.get(f'/api/documents/{document_id}').json()['content_id']))
    backup = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': True}))
    old_hash = file_hash(source)
    source.parent.rename(tmp_path/'offline-source')
    recovered = finish(client, client.post(f'/api/data/backups/{backup["id"]}/recover-workspace', headers=auth))
    root = Path(recovered['workspace_root'])
    assert recovered['recovery_ready'] and recovered['recovered_pdfs'] == 1
    assert validate_workspace(root) == root
    with sqlite3.connect(root/'db/literature-workbench.sqlite3') as c:
        doc = c.execute('SELECT id,path FROM documents WHERE id=?', (document_id,)).fetchone()
        assert doc[0] == document_id and Path(doc[1]).is_relative_to(root)
        assert file_hash(Path(doc[1])) == old_hash
        assert c.execute('SELECT note FROM research_entries').fetchone()[0] == 'Do not lose this.'
        assert c.execute('SELECT translated_text FROM translation_cache').fetchone()[0] == '已缓存译文'
        assert c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 0
        assert c.execute('SELECT error_code FROM jobs WHERE dedup_key=?', ('old-paid-work',)).fetchone()[0] == 'workspace_restored'
        assert c.execute('SELECT page FROM reading_records').fetchone()[0] == 1
        assert c.execute('SELECT output_dir FROM parse_runs WHERE id=?', (run_id,)).fetchone()[0] == 'parse-results/example-run'
        assert (root/'parse-results/example-run/source.txt').read_text(encoding='utf-8') == 'Saved parser attachment'
        for (path,) in c.execute('SELECT backup_path FROM pdf_annotation_mutations UNION SELECT backup_path FROM pdf_annotation_baselines'):
            assert Path(path).is_relative_to(root) and io_path(Path(path)).is_file()
    session = LocalSession(cookie_name='lw_recovery_test')
    with TestClient(create_app(AppSettings(data_root=root), local_session=session, secret_store=secret_store, register_initial_library=False)) as clone:
        assert clone.post('/api/session/bootstrap', headers={'Origin': 'http://127.0.0.1'}, json={'token': session.issue_bootstrap_token()}).status_code == 204
        assert 'lw_recovery_test' in clone.cookies and 'lw_session' not in clone.cookies
        assert clone.get(f'/api/documents/{document_id}/file').status_code == 200
        listed = clone.get(f'/api/documents/{document_id}/annotations')
        assert listed.status_code == 200, listed.text
        annotations = listed.json()['items']
        assert len(annotations) == 1 and annotations[0]['comment'] == 'Saved annotation'
        current = clone.get(f'/api/documents/{document_id}').json()
        payload.update(document_version_id=current['current_document_version_id'], content_id=current['content_id'], comment='Clone edit')
        response = clone.post(f'/api/documents/{document_id}/annotations', headers={'Origin': 'http://127.0.0.1', 'X-LW-CSRF': session.csrf_token}, json=payload)
        assert response.status_code == 201, response.text
        clone_auth = {'Origin': 'http://127.0.0.1', 'X-LW-CSRF': session.csrf_token}
        assert clone.post(f'/api/documents/{document_id}/annotations/undo', headers=clone_auth).status_code == 200
        assert len(clone.get(f'/api/documents/{document_id}/annotations').json()['items']) == 1
        # Undo again uses a snapshot from before recovery, rather than a newly created file.
        assert clone.post(f'/api/documents/{document_id}/annotations/undo', headers=clone_auth).status_code == 200
        assert not clone.get(f'/api/documents/{document_id}/annotations').json()['items']
        assert clone.get('/api/workspace/notebook').json()['items'][0]['note'] == 'Do not lose this.'
        assert clone.get('/api/data/context').json()['recovered']
    assert file_hash(tmp_path/'offline-source/paper.pdf') == old_hash
    assert client.get('/api/data/overview').json()['recoveries'][0]['id'] == recovered['id']


def test_database_only_can_preview_but_cannot_become_a_ready_workspace(client, authenticated):
    auth = headers(authenticated)
    backup = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': False}))
    failed = finish(client, client.post(f'/api/data/backups/{backup["id"]}/recover-workspace', headers=auth), completed=False)
    assert failed['error'] == 'recovery_full_backup_required'
    assert client.get(f'/api/data/jobs/{backup["id"]}').json()['verification_status'] == 'passed'
    assert client.post(f'/api/data/recoveries/{failed["id"]}/open', headers=auth).status_code == 503


def test_open_requires_explicit_request_and_a_ready_operation_owned_by_this_workspace(client, authenticated, tmp_path, monkeypatch):
    from literature_workbench import launcher
    calls = []
    monkeypatch.setattr(launcher, 'open_recovered_workspace', calls.append)
    seed_pdf(client, tmp_path)
    auth = headers(authenticated)
    backup = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': True}))
    recovery = finish(client, client.post(f'/api/data/backups/{backup["id"]}/recover-workspace', headers=auth))
    assert not calls
    url = f'/api/data/recoveries/{recovery["id"]}/open'
    assert client.post(url).status_code == 403
    assert not calls
    assert client.post(url, headers=auth).json()['requested']
    assert calls == [Path(recovery['workspace_root'])]
    assert client.post(f'/api/data/recoveries/{backup["id"]}/open', headers=auth).status_code == 503
    stopped = []
    monkeypatch.setattr(launcher, 'stop_running_instance', lambda manifest: stopped.append(manifest) or True)
    stop_url = f'/api/data/recoveries/{recovery["id"]}/stop'
    assert client.post(stop_url).status_code == 403
    assert not stopped
    assert client.post(stop_url, headers=auth).json()['stopped']
    assert stopped == [Path(recovery['workspace_root'])/'runtime/launcher/launcher.json']


def test_invalid_or_relocated_marker_and_external_paths_cannot_launch(client, authenticated, tmp_path):
    seed_pdf(client, tmp_path)
    auth = headers(authenticated)
    backup = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': True}))
    recovery = finish(client, client.post(f'/api/data/backups/{backup["id"]}/recover-workspace', headers=auth))
    data = Path(recovery['workspace_root'])
    with sqlite3.connect(data/'db/literature-workbench.sqlite3') as c:
        c.execute('UPDATE documents SET path=?', (str(tmp_path/'external.pdf'),))
    with pytest.raises(BackupError, match='recovery_invalid_workspace'):
        validate_workspace(data)
    marker = data.parent/'recovered-workspace.json'
    value = json.loads(marker.read_text(encoding='utf-8'))
    value['ready'] = False
    marker.write_text(json.dumps(value), encoding='utf-8')
    assert client.post(f'/api/data/recoveries/{recovery["id"]}/open', headers=auth).status_code == 503


def test_managed_imports_are_reconnected_and_new_imports_and_linked_folders_still_work(client, authenticated, tmp_path, secret_store):
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 60), 'Managed import content')
        body = pdf.tobytes()

    def upload(target, auth):
        intent = target.post('/api/imports/intents', headers=auth, json={'original_name': 'Imported.pdf', 'size': len(body), 'media_type': 'application/pdf'}).json()
        response = target.post(f'/api/imports/{intent["intent_id"]}/content', headers={**auth, 'Content-Type': 'application/pdf', 'X-LW-Upload-Token': intent['upload_token']}, content=body)
        assert response.status_code == 202, response.text
        target.app.state.import_validator.validate_and_promote(UUID(intent['intent_id']))
        return intent

    auth = headers(authenticated)
    upload(client, auth)
    docs = client.get('/api/documents').json()['items']
    assert len(docs) == 1
    original_id = docs[0]['document_id']
    # A separate incomplete upload must never resume automatically in the clone.
    pending = client.post('/api/imports/intents', headers=auth, json={'original_name': 'Pending.pdf', 'size': len(body), 'media_type': 'application/pdf'}).json()
    assert client.post(f'/api/imports/{pending["intent_id"]}/content', headers={**auth, 'Content-Type': 'application/pdf', 'X-LW-Upload-Token': pending['upload_token']}, content=body).status_code == 202
    backup = finish(client, client.post('/api/data/backups', headers=auth, json={'include_pdfs': True}))
    recovery = finish(client, client.post(f'/api/data/backups/{backup["id"]}/recover-workspace', headers=auth))
    root = Path(recovery['workspace_root'])
    session = LocalSession(cookie_name='lw_recovery_managed')
    with TestClient(create_app(AppSettings(data_root=root), local_session=session, secret_store=secret_store, register_initial_library=False)) as clone:
        clone.post('/api/session/bootstrap', headers={'Origin': 'http://127.0.0.1'}, json={'token': session.issue_bootstrap_token()})
        clone_auth = {'Origin': 'http://127.0.0.1', 'X-LW-CSRF': session.csrf_token}
        assert clone.get(f'/api/documents/{original_id}/file').status_code == 200
        with clone.app.state.database.connect() as c:
            assert c.execute('SELECT status FROM import_intents WHERE id=?', (pending['intent_id'],)).fetchone()[0] == 'failed'
        upload(clone, clone_auth)
        assert [d['document_id'] for d in clone.get('/api/documents').json()['items']] == [original_id]
        folder = tmp_path/'new-linked-folder'
        folder.mkdir()
        (folder/'new.pdf').write_bytes(body)
        state = clone.app.state
        library = state.library_repository.register(state.file_inspector.inspect_root_directory(folder), recursive=True, authorization_source='user_selected_directory')
        state.library_scanner.scan_library(library.id)
        assert clone.get('/api/documents').json()['total'] == 2
        assert validate_workspace(root) == root
