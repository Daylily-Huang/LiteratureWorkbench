import json
from uuid import uuid4

from literature_workbench.api.workspace import _entry, archive_answer
from literature_workbench.features.research.evidence_matrix import export_chunks
from test_workspace import seed


def test_matrix_pages_all_history_without_sending_answers_and_searches_all_fields(client, authenticated, tmp_path):
    document = seed(client, tmp_path)
    other = str(uuid4())
    db = client.app.state.database
    ids = [archive_answer(db, f"Problem {i:03}", {'answer': 'Large answer ' * 4000, 'provider': 'controlled', 'model': 'test-model'}, [document]) for i in range(45)]
    with db.transaction() as connection:
        connection.execute('UPDATE research_entries SET title=?,note=? WHERE id=?', ('Old special title', 'Straße 样本 100%_', ids[0]))
    quoted = archive_answer(db, 'Quote only membership', {'answer': 'Unique stored conclusion', 'citations': [{'document_id': document, 'content_id': 'f' * 64, 'excerpt': 'Unique literal quote', 'physical_page': 1}]}, [other])
    archive_answer(db, 'Unrelated', {'answer': 'Unique stored conclusion'}, [other])
    url = '/api/workspace/notebook/evidence'
    params = {'document_ids': document}
    first = client.get(url, params=params)
    assert first.status_code == 200 and first.headers['cache-control'] == 'no-store'
    page = first.json()
    assert page['total'] == 46 and page['has_more'] and len(page['items']) == 20
    assert 'Large answer' not in first.text and all('answer' not in item for item in page['items'])
    assert next(item for item in page['items'] if item['id'] == quoted)['citations'][0]['source_changed'] is True
    pages = [page, client.get(url, params={**params, 'offset': 20}).json(), client.get(url, params={**params, 'offset': 40}).json()]
    assert len({item['id'] for p in pages for item in p['items']}) == 46 and not pages[-1]['has_more']
    for query in ['old special', 'Straße', '样本 100%_', 'Problem 000 Straße']:
        found = client.get(url, params={**params, 'q': query}).json()
        assert found['total'] == 1 and found['items'][0]['id'] == ids[0]
    for query in ['literal quote', 'unique stored']:
        found = client.get(url, params={**params, 'q': query}).json()
        assert found['total'] == 1 and found['items'][0]['id'] == quoted
    assert client.get(url, params={'document_ids': str(uuid4())}).json()['total'] == 0


def test_export_includes_all_matching_records_original_answer_and_outside_sources(client, authenticated, tmp_path):
    document = seed(client, tmp_path)
    db = client.app.state.database
    outside = str(uuid4())
    with db.connect() as c:
        content = c.execute('SELECT content_sha256 FROM documents WHERE id=?', (document,)).fetchone()[0]
    body = {'answer': 'Original full answer <script>not HTML</script>', 'provider': 'controlled', 'model': 'fixture', 'scope': 'selected',
            'reading_coverage': {'historical_snapshot': 'unchanged'}, 'selection_context': {'excerpt': 'original selection'},
            'scope_documents': [{'document_id': document, 'display_name': 'paper.pdf', 'cited': True}, {'document_id': outside, 'display_name': 'outside.pdf', 'cited': False}],
            'citations': [{'document_id': document, 'display_name': 'paper.pdf', 'content_id': content, 'excerpt': 'Exact primary evidence', 'physical_page': 1, 'rects': [[1, 2, 3, 4]]},
                          {'document_id': outside, 'display_name': 'outside.pdf', 'content_id': 'b' * 64, 'excerpt': 'Outside original evidence', 'physical_page': 9}]}
    ids = [archive_answer(db, f'Compare <test> {i:02}', body, [document, outside]) for i in range(25)]
    with db.transaction() as c:
        c.execute('UPDATE research_entries SET note=? WHERE id=?', ('My own interpretation', ids[0]))
    url = '/api/workspace/notebook/evidence/export'
    params = [('document_ids', document), ('format', 'json')]
    response = client.get(url, params=params)
    assert response.status_code == 200 and response.headers['x-content-type-options'] == 'nosniff'
    exported = response.json()
    assert exported['format'] == 'literature-workbench-evidence-matrix' and exported['total'] == 25
    assert len(exported['entries']) == 25 and set(e['id'] for e in exported['entries']) == set(ids)
    entry = next(e for e in exported['entries'] if e['id'] == ids[0])
    assert entry['note'] == 'My own interpretation' and entry['document_ids'] == [document, outside]
    assert entry['answer']['answer'] == body['answer'] and entry['answer']['reading_coverage'] == body['reading_coverage']
    a, b = entry['answer']['citations']
    assert a['source_changed'] is False and b['source_changed'] is True and b['excerpt'] == 'Outside original evidence'
    assert a['rects'] == [[1, 2, 3, 4]]
    markdown = client.get(url, params={'document_ids': document, 'q': 'interpretation', 'format': 'markdown'}).text
    for value in ['记录数：1', '版本：0', 'Original full answer', 'My own interpretation', 'Outside original evidence', 'test', 'fixture', content, outside, '原选篇 ID', 'original selection', 'historical', '已变化或不可用']:
        assert value in markdown
    assert '<script>' not in markdown and '&lt;script&gt;' in markdown
    with db.connect() as c:
        assert json.loads(c.execute('SELECT answer_json FROM research_entries WHERE id=?', (ids[0],)).fetchone()[0]) == body


def test_export_snapshot_keeps_count_rows_and_source_status_consistent_during_other_edits(client, authenticated, tmp_path):
    document = seed(client, tmp_path)
    db = client.app.state.database
    with db.connect() as c:
        content = c.execute('SELECT content_sha256 FROM documents WHERE id=?', (document,)).fetchone()[0]
    entry_id = archive_answer(db, 'Snapshot', {'citations': [{'document_id': document, 'content_id': content, 'excerpt': 'Old evidence'}]}, [document])
    chunks = export_chunks(db.path, [document], '', 'json', _entry)
    prefix = next(chunks)
    with db.transaction() as c:
        c.execute('UPDATE research_entries SET note=? WHERE id=?', ('New thinking', entry_id))
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (document,))
    archive_answer(db, 'Later answer', {'answer': 'later'}, [document])
    exported = json.loads(prefix + ''.join(chunks))
    assert exported['total'] == 1 and len(exported['entries']) == 1
    assert exported['entries'][0]['note'] == '' and exported['entries'][0]['answer']['citations'][0]['source_changed'] is False
    assert exported['documents'][0]['available'] is True
    latest = client.get('/api/workspace/notebook/evidence', params={'document_ids': document}).json()
    assert latest['total'] == 2
    assert next(e for e in latest['items'] if e['id'] == entry_id)['citations'][0]['source_changed'] is True
    interrupted = export_chunks(db.path, [document], '', 'markdown', _entry)
    next(interrupted)
    interrupted.close()
    assert interrupted.gi_frame is None


def test_matrix_requires_session_and_bounded_valid_scope_and_query(client, authenticated):
    urls = ['/api/workspace/notebook/evidence', '/api/workspace/notebook/evidence/export']
    for url in urls:
        for params in [{}, {'document_ids': 'bad'}, [('document_ids', str(uuid4())) for _ in range(9)], {'document_ids': str(uuid4()), 'q': 'x' * 2001}]:
            assert client.get(url, params=params).status_code == 422
    document = str(uuid4())
    assert client.get(urls[0], params={'document_ids': document, 'limit': 51}).status_code == 422
    assert client.get(urls[0], params={'document_ids': document, 'offset': -1}).status_code == 422
    assert client.get(urls[1], params={'document_ids': document, 'format': 'html'}).status_code == 422
    assert client.get(urls[1], params={'document_ids': document, 'format': 'json'}).json()['total'] == 0
    client.cookies.clear()
    for url in urls:
        assert client.get(url, params={'document_ids': document}).status_code == 403
