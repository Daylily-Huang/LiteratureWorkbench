import json
from uuid import uuid4

import pytest

from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.research.full_text_relations import FullTextRelations
from test_library_service import _seed_document, _service, FakeDeepSeek


ALPHA = 'Alpha repeated visits improve detection estimates.'
BETA = 'Beta single visits confound detection and abundance.'


def result(source=ALPHA, target=BETA, target_ref='[d2 p2]'):
    return json.dumps({'summary': 'Conditional method comparison', 'limitations': [], 'candidates': [{
        'source_ref': '[d1 p2]', 'target_ref': target_ref, 'source_quote': source, 'target_quote': target,
        'relation': 'method', 'difference_basis': 'method', 'rationale': 'Sampling designs differ under these conditions.',
        'comparison_note': 'Compare detection assumptions before interpreting estimates.', 'caveats': ['Verify population comparability.']}]})


def fixture(database):
    a = _seed_document(database, content_id='a'*64, display_name='Alpha.pdf', page_texts=[['Alpha background.'], [ALPHA, 'Alpha additional unprovided quotation exists elsewhere.']])
    b = _seed_document(database, content_id='b'*64, display_name='Beta.pdf', page_texts=[['Beta background.'], [BETA]])
    model = FakeDeepSeek(result=result())
    service = _service(database, model)
    return [a, b], service, model


def run(service, ids, question='Compare methods', force=False):
    relations = service.full_text_relations()
    prepared = relations.prepare(ids, question, force)
    return relations.analyze(ids, question, force, prepared['source_versions'], prepared['plan_id'])


def test_full_relation_study_sends_all_pages_checks_evidence_and_creates_reusable_notes(research_db):
    ids, service, model = fixture(research_db)
    ready = service.full_text_relations().prepare(ids, 'Compare methods', False)
    assert not model.calls and all(d['supplied_pages']==2 for d in ready['coverage'])
    answer, summary, warnings, proposals = run(service, ids)
    assert len(model.calls)==1 and len(proposals)==1
    sent = json.loads(model.calls[0]);assert all(len(d['pages'])==2 for d in sent['sources'])
    assert 'retrieved fragments' not in model.system_prompts[0]
    assert answer.reading_mode=='full_text' and answer.reading_coverage['cache_saved']
    assert set(proposals[0]['reading_origin']['source_versions'])==set(ids)
    plan = service.full_text_relations().prepare(ids, 'Another comparison', False)
    assert set(plan['source_modes'].values())=={'analysis_reuse'} and len(model.calls)==1
    ordinary = service.prepare_full_text(question='Compare methods', document_ids=ids)
    assert ordinary['cache_status']=='miss'  # A relation rationale is not an exact Q&A answer.
    with research_db.connect() as c:
        assert c.execute('SELECT count(*) FROM relation_candidates').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM full_text_study_cache').fetchone()[0]==1


def test_new_paper_uses_only_its_full_text_and_guessing_unsupplied_old_quote_is_rejected(research_db):
    ids, service, model = fixture(research_db);run(service, ids)
    third = _seed_document(research_db,content_id='c'*64,display_name='Gamma.pdf',page_texts=[['Gamma context.'],['Gamma validates the repeated sampling design.']])
    new_ids=[*ids,third]
    model.result=result(source='Alpha additional unprovided quotation exists elsewhere.',target='Gamma validates the repeated sampling design.',target_ref='[d3 p2]')
    _, _, warnings, proposals=run(service,new_ids,'Compare with Gamma')
    sent=json.loads(model.calls[-1]);assert len(sent['new_documents'])==1
    assert sent['new_documents'][0]['filename']=='Gamma.pdf'
    assert sent['reading_modes']=={'d1':'analysis_reuse','d2':'analysis_reuse','d3':'full_text'}
    assert not proposals and any('本次提供的引句范围' in w for w in warnings)


def test_plan_changes_do_not_call_model_and_force_requires_a_new_full_plan(research_db):
    ids,service,model=fixture(research_db)
    relations=service.full_text_relations();old=relations.prepare(ids,'Compare methods',False)
    run(service,ids)
    with pytest.raises(FullTextError,match='full_text_plan_changed'):
        relations.analyze(ids,'Compare methods',False,old['source_versions'],old['plan_id'])
    assert len(model.calls)==1
    forced=relations.prepare(ids,'Compare methods',True)
    assert set(forced['source_modes'].values())=={'full_text'}
    assert 'pages' in json.loads(relations.plan(ids,'Compare methods',True).context.prompt)['sources'][0]


def test_api_full_mode_is_separate_from_fragments_reuses_completed_analysis_and_keeps_graph_empty(client,authenticated,monkeypatch):
    ids,service,model=fixture(client.app.state.database)
    monkeypatch.setattr(client.app.state,'research_service',service)
    h={'Origin':'http://testserver','X-LW-CSRF':authenticated.csrf}
    body={'question':'Compare methods','document_ids':ids}
    path='/api/workspace/analyses'
    assert client.post(path+'/prepare',json=body).status_code==403
    ready=client.post(path+'/prepare',headers=h,json=body).json()
    assert not model.calls
    request={**body,'reading_mode':'full_text','source_versions':ready['source_versions'],'plan_id':ready['plan_id'],'request_id':str(uuid4())}
    first=client.post(path,headers=h,json=request);assert first.status_code==200,first.text
    again=client.post(path,headers=h,json={**request,'request_id':str(uuid4())})
    assert again.json()['id']==first.json()['id'] and again.json()['cache_hit'] is True and len(model.calls)==1
    analysis=client.get(path+'/'+first.json()['id']).json()['items'][0]
    assert analysis['reading_coverage']['reading_mode']=='full_text' and len(analysis['candidates'])==1
    assert not any(e['kind']=='manual' for e in client.get('/api/workspace/graph').json()['edges'])
    assert client.post(path,headers=h,json={**request,'reading_mode':'retrieval'}).status_code==409
    assert client.post(path,headers=h,json={**body,'reading_mode':'full_text','request_id':str(uuid4())}).status_code==422


def test_plan_recheck_after_new_notes_can_retry_without_force_or_a_second_model_call(client,authenticated,monkeypatch):
    ids,service,model=fixture(client.app.state.database)
    monkeypatch.setattr(client.app.state,'research_service',service)
    h={'Origin':'http://testserver','X-LW-CSRF':authenticated.csrf};body={'question':'Changed plan','document_ids':ids}
    path='/api/workspace/analyses'
    old=client.post(path+'/prepare',headers=h,json=body).json()
    run(service,ids,'A different earlier study')
    request={**body,'reading_mode':'full_text','source_versions':old['source_versions'],'plan_id':old['plan_id'],'request_id':str(uuid4())}
    assert client.post(path,headers=h,json=request).json()['code']=='full_text_plan_changed'
    assert len(model.calls)==1
    fresh=client.post(path+'/prepare',headers=h,json=body).json()
    response=client.post(path,headers=h,json={**request,'request_id':str(uuid4()),'plan_id':fresh['plan_id'],'source_versions':fresh['source_versions']})
    assert response.status_code==200 and response.json()['status']=='completed' and len(model.calls)==2


def test_uncited_third_paper_version_blocks_full_relation_confirmation(client,authenticated,monkeypatch):
    db=client.app.state.database;ids,service,model=fixture(db)
    third=_seed_document(db,content_id='c'*64,display_name='Gamma.pdf',page_texts=[['Gamma context.']])
    ids.append(third);monkeypatch.setattr(client.app.state,'research_service',service)
    h={'Origin':'http://testserver','X-LW-CSRF':authenticated.csrf};body={'question':'Three-paper comparison','document_ids':ids};path='/api/workspace/analyses'
    ready=client.post(path+'/prepare',headers=h,json=body).json()
    saved=client.post(path,headers=h,json={**body,'reading_mode':'full_text','source_versions':ready['source_versions'],'plan_id':ready['plan_id'],'request_id':str(uuid4())}).json()
    with db.transaction() as c:c.execute("UPDATE documents SET status='missing' WHERE id=?",(third,))
    analysis=client.get(path+'/'+saved['id']).json()['items'][0];candidate=analysis['candidates'][0]
    assert candidate['source_changed']
    decision={'revision':0,'confirmed':True,'relation':'method','difference_basis':'method','note':'Checked','comparison_note':'Checked'}
    assert client.post('/api/workspace/candidates/'+candidate['id']+'/accept',headers=h,json=decision).json()['code']=='source_changed'
