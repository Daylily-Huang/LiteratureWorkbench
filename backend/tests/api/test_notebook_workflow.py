from uuid import uuid4

from literature_workbench.api.workspace import archive_answer
from test_workspace import headers, seed


def test_index_is_paged_small_and_searches_all_records_without_loading_full_answers(client, authenticated):
    db = client.app.state.database
    ids = [archive_answer(db, f'研究记录 {i:02}', {'answer': 'long answer ' * 2000, 'citations': []}, []) for i in range(45)]
    with db.transaction() as c:
        c.execute('UPDATE research_entries SET note=?,starred=1 WHERE id=?', ('STRASSE 样本 100%_证据', ids[0]))
    first = client.get('/api/workspace/notebook/index').json()
    assert first['total'] == 45 and first['has_more'] and len(first['items']) == 30
    assert len(first['workspace_id']) == 64
    assert all('answer' not in entry and 'note' not in entry for entry in first['items'])
    second = client.get('/api/workspace/notebook/index?offset=30').json()
    assert len(second['items']) == 15 and not second['has_more']
    assert set(e['id'] for e in first['items']).isdisjoint(e['id'] for e in second['items'])
    for query in ['q=研究记录%20样本', 'q=Straße', 'q=100%25_', 'starred=true']:
        result = client.get('/api/workspace/notebook/index?' + query).json()
        assert result['total'] == 1 and result['items'][0]['id'] == ids[0]
    detail = client.get(f'/api/workspace/notebook/{ids[0]}').json()
    assert detail['answer']['answer'] == 'long answer ' * 2000
    assert detail['note'] == 'STRASSE 样本 100%_证据'
    assert client.get('/api/workspace/notebook/index?limit=101').status_code == 422
    assert client.get('/api/workspace/notebook/index?offset=-1').status_code == 422
    assert client.get('/api/workspace/notebook/index?q=' + 'x' * 2001).status_code == 422
    long_question = ' '.join(['A long research question'] * 15)
    long_id = archive_answer(db, long_question, {'answer': 'A'}, [])
    assert client.get('/api/workspace/notebook/index', params={'q': long_question}).json()['items'][0]['id'] == long_id


def test_index_includes_quote_search_and_detail_marks_current_source_status(client, authenticated, tmp_path):
    doc = seed(client, tmp_path)
    db = client.app.state.database
    with db.connect() as c:
        content = c.execute('SELECT content_sha256 FROM documents WHERE id=?', (doc,)).fetchone()[0]
    body = {'answer': 'conclusion', 'citations': [{'document_id': doc, 'content_id': content, 'excerpt': 'Unique literal quote', 'physical_page': 1}]}
    entry_id = archive_answer(db, 'Study', body, [doc])
    assert client.get('/api/workspace/notebook/index?q=literal').json()['items'][0]['id'] == entry_id
    url = f'/api/workspace/notebook/{entry_id}'
    assert client.get(url).json()['answer']['citations'][0]['source_changed'] is False
    with db.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (doc,))
    detail = client.get(url).json()
    assert detail['answer']['citations'][0]['source_changed'] is True
    assert detail['answer']['citations'][0]['excerpt'] == 'Unique literal quote'
    assert client.get(f'/api/workspace/notebook/{uuid4()}').status_code == 404
    client.cookies.clear()
    assert client.get(url).status_code == 403
    assert client.get('/api/workspace/notebook/index').status_code == 403


def test_new_note_retry_is_idempotent_and_cannot_overwrite_existing_note_or_answer(client, authenticated):
    note_id = str(uuid4())
    payload = {'title': '断线草稿', 'note': '只创建一次', 'note_id': note_id}
    assert client.post('/api/workspace/notebook', headers=headers(authenticated), json=payload).status_code == 201
    retry = client.post('/api/workspace/notebook', headers=headers(authenticated), json=payload)
    assert retry.status_code == 200 and retry.json()['id'] == note_id
    assert client.get('/api/workspace/notebook/index').json()['total'] == 1
    assert client.post('/api/workspace/notebook', headers=headers(authenticated), json={**payload, 'note': '改写旧笔记'}).status_code == 409
    assert client.patch(f'/api/workspace/notebook/{note_id}', headers=headers(authenticated), json={**payload, 'revision': 0, 'starred': True}).status_code == 422
    ai_id = archive_answer(client.app.state.database, 'AI problem', {'answer': 'must remain'}, [])
    assert client.post('/api/workspace/notebook', headers=headers(authenticated), json={**payload, 'note_id': ai_id}).status_code == 409
    assert client.get(f'/api/workspace/notebook/{ai_id}').json()['answer']['answer'] == 'must remain'
    assert client.post('/api/workspace/notebook', json=payload).status_code == 403


def test_conflict_copy_retains_original_answer_scope_and_evidence_without_overwriting_either(client, authenticated):
    db = client.app.state.database
    source_id = str(uuid4())
    body = {'answer': 'Original conclusion', 'scope': 'selected', 'reading_coverage': {'sentinel': 'preserved'},
            'citations': [{'document_id': source_id, 'content_id': 'f' * 64, 'excerpt': 'Original evidence', 'physical_page': 4}]}
    original = archive_answer(db, 'Compare', body, [source_id])
    assert client.patch(f'/api/workspace/notebook/{original}', headers=headers(authenticated), json={'title': 'Other window', 'note': 'Newer thinking', 'starred': True, 'revision': 0}).status_code == 200
    payload = {'note_id': str(uuid4()), 'title': 'Conflict copy', 'note': 'Unsent local thinking'}
    url = f'/api/workspace/notebook/{original}/copy'
    copied = client.post(url, headers=headers(authenticated), json=payload)
    assert copied.status_code == 200
    item = copied.json()
    assert item['document_ids'] == [source_id] and item['note'] == payload['note']
    assert item['answer']['answer'] == body['answer'] and item['answer']['reading_coverage'] == body['reading_coverage']
    assert item['answer']['citations'][0]['excerpt'] == 'Original evidence'
    assert item['answer']['citations'][0]['source_changed'] is True
    assert item['answer']['copied_from_entry_id'] == original
    assert client.post(url, headers=headers(authenticated), json=payload).json()['id'] == item['id']
    assert client.post(url, headers=headers(authenticated), json={**payload, 'note': 'different'}).status_code == 409
    unchanged = client.get(f'/api/workspace/notebook/{original}').json()
    assert unchanged['note'] == 'Newer thinking' and unchanged['revision'] == 1
    assert 'copied_from_entry_id' not in unchanged['answer']
    assert client.post(url, json=payload).status_code == 403
