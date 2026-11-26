"""Study-to-graph provenance, idempotency and human confirmation boundaries."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.research.study_relations import draft_from_study
from test_ordered_study import warm
from test_library_service import _seed_document


def key(database):
    with database.connect() as c:
        return c.execute('SELECT cache_key FROM full_text_study_cache').fetchone()[0]


def test_cached_claim_transfer_is_local_idempotent_and_keeps_full_context(research_db):
    a, b, question, service, model, _ = warm(research_db)
    cache = key(research_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: draft_from_study(research_db, cache, 0, 0, 1), range(2)))
    assert len({r['id'] for r in results}) == 1 and sorted(r['cache_hit'] for r in results) == [False, True]
    assert len(model.calls) == 1
    assert all(r['api_call_required'] is False and r['document_ids'] == [a, b] for r in results)
    with research_db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_relations').fetchone()[0] == 0
        proposal = json.loads(c.execute('SELECT proposal_json FROM relation_candidates').fetchone()[0])
        assert proposal['source']['document_id'] == a and proposal['target']['document_id'] == b
        assert proposal['relation'] == 'related' and proposal['difference_basis'] == 'unspecified'
        assert proposal['study_origin']['conclusion'] == 'Original first-to-second conclusion'
        assert [d['document_id'] for d in proposal['study_origin']['documents']] == [a, b]
        assert c.execute('SELECT question FROM relation_analyses').fetchone()[0] == question
    reverse = draft_from_study(research_db, cache, 0, 1, 0)
    assert reverse['id'] != results[0]['id']


def test_changed_or_corrupt_study_and_invalid_pair_never_create_candidates(research_db):
    a, b, *_ = warm(research_db)
    cache = key(research_db)
    with pytest.raises(FullTextError, match='study_two_documents_required'):
        draft_from_study(research_db, cache, 0, 0, 0)
    with pytest.raises(FullTextError, match='study_evidence_selection_invalid'):
        draft_from_study(research_db, cache, 20, 0, 1)
    with research_db.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (b,))
    with pytest.raises(FullTextError, match='study_evidence_changed'):
        draft_from_study(research_db, cache, 0, 0, 1)
    with research_db.transaction() as c:
        c.execute("UPDATE documents SET status='valid_pdf' WHERE id=?", (b,))
        c.execute("UPDATE full_text_study_cache SET output_json='{}'")
    with pytest.raises(FullTextError, match='study_evidence_changed'):
        draft_from_study(research_db, cache, 0, 0, 1)
    with research_db.connect() as c:
        assert c.execute('SELECT count(*) FROM relation_candidates').fetchone()[0] == 0


def test_api_requires_csrf_revalidates_server_evidence_and_preserves_review(client, authenticated, monkeypatch):
    db = client.app.state.database
    a, b, _, _, model, _ = warm(db)
    monkeypatch.setattr(client.app.state.research_service, 'ask_library', lambda **_: pytest.fail('Unexpected AI call'))
    body = {'cache_key': key(db), 'claim_index': 0, 'source_index': 0, 'target_index': 1}
    headers = {'Origin': 'http://testserver', 'X-LW-CSRF': authenticated.csrf}
    path = '/api/workspace/analyses/from-study'
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, headers=headers, json={**body, 'source_index': -1}).status_code == 422
    assert client.post(path, headers=headers, json={**body, 'source': {'excerpt': 'fake'}}).status_code == 422
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200
    saved = response.json()
    analysis = client.get('/api/workspace/analyses/'+saved['id']).json()['items'][0]
    candidate = analysis['candidates'][0]
    assert not candidate['source_changed']
    decision = {'revision': 0, 'confirmed': True, 'relation': 'method', 'difference_basis': 'method',
                'note': 'Human checked sampling designs.', 'comparison_note': 'Human checked conditions.'}
    accepted = client.post(f"/api/workspace/candidates/{candidate['id']}/accept", headers=headers, json=decision)
    assert accepted.status_code == 200
    repeated = client.post(path, headers=headers, json=body).json()
    assert repeated['id'] == saved['id'] and repeated['cache_hit'] is True
    graph = client.get('/api/workspace/graph').json()
    edge = next(e for e in graph['edges'] if e['kind'] == 'manual')
    assert edge['relation'] == 'method' and edge['evidence']['provider'] == 'study_cache'
    assert edge['evidence']['ai_proposal']['study_origin']['cache_key'] == body['cache_key']
    assert len(model.calls) == 1


def test_uncited_third_paper_remains_part_of_original_context(client, authenticated):
    db = client.app.state.database
    a, b, _, service, model, _ = warm(db)
    third = _seed_document(db, content_id='c'*64, display_name='C.pdf', page_texts=[['Third paper context.']])
    ready = service.prepare_full_text(question='Compare all three', document_ids=[a, b, third])
    service.ask_full_text(question='Compare all three', document_ids=[a, b, third], source_versions=ready['source_versions'], plan_id=ready['plan_id'])
    with db.connect() as c:
        cache = c.execute("SELECT cache_key FROM full_text_study_cache WHERE question='Compare all three'").fetchone()[0]
    saved = draft_from_study(db, cache, 0, 0, 1)
    h = {'Origin': 'http://testserver', 'X-LW-CSRF': authenticated.csrf}
    accepted = draft_from_study(db, cache, 0, 1, 0)
    decision = {'revision': 0, 'confirmed': True, 'relation': 'supports', 'difference_basis': 'claim', 'note': 'checked', 'comparison_note': 'checked'}
    assert client.post(f"/api/workspace/candidates/{accepted['candidate_id']}/accept", headers=h, json=decision).status_code == 200
    with db.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (third,))
    analysis = client.get('/api/workspace/analyses/'+saved['id']).json()['items'][0]
    assert len(analysis['document_ids']) == 3 and analysis['candidates'][0]['source_changed']
    assert client.post(f"/api/workspace/candidates/{saved['candidate_id']}/accept", headers=h, json=decision).json()['code'] == 'source_changed'
    graph = client.get('/api/workspace/graph').json()
    assert next(e for e in graph['edges'] if e['kind'] == 'manual')['source_changed'] is True
    with pytest.raises(FullTextError, match='study_evidence_changed'):
        draft_from_study(db, cache, 0, 0, 1)


def test_unchanged_text_with_new_parse_can_be_revalidated_into_a_new_current_draft(research_db):
    from conftest import make_page, publish_pages

    warm(research_db)
    cache = key(research_db)
    old = draft_from_study(research_db, cache, 0, 0, 1)
    publish_pages(research_db, 'a'*64, [make_page(1, ['Alpha verified source quotation.'])])
    fresh = draft_from_study(research_db, cache, 0, 0, 1)
    assert fresh['id'] != old['id']
    assert draft_from_study(research_db, cache, 0, 0, 1)['id'] == fresh['id']
    with research_db.connect() as c:
        assert c.execute('SELECT count(*) FROM relation_candidates').fetchone()[0] == 2
