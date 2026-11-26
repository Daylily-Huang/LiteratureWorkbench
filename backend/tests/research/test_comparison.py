import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from literature_workbench.features.research.comparison import Comparison, FIELDS
from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.settings.repository import SettingsRepository
from test_library_service import _seed_document, FakeDeepSeek

TEXT = ['Alpha used repeated visits at twelve sites.', 'Beta used single visits at twenty sites.', 'Gamma used paired visits at eight sites.']


def output(count=2):
    return json.dumps({'cells': [{'document': f'd{i+1}', 'field': field, 'status': 'supported',
        'value': TEXT[i], 'evidence': [f'd{i+1}p2s1']}
        for i in range(count) for field in FIELDS]})


def setup(db):
    ids = [_seed_document(db, content_id=chr(97+i)*64, display_name=f'Paper-{i}.pdf',
        page_texts=[['Study background.'], [TEXT[i]], ['An unprovided conclusion exists elsewhere on this page.']]) for i in range(2)]
    model = FakeDeepSeek(result=output())
    service = Comparison(db, RagChunkRepository(db), SettingsRepository(db), model, 'gen1')
    return ids, model, service


def run(service, ids, question='Compare methods', force=False, request_id=None):
    ready = service.prepare(ids, question, force)
    return service.analyze(request_id or str(uuid4()), ids, question, force, ready['source_versions'], ready['plan_id'])


def test_complete_table_is_verified_cached_and_never_writes_notes_or_graph(research_db):
    ids, model, service = setup(research_db)
    ready = service.prepare(ids, 'Compare methods', False)
    assert not model.calls and ready['cache_status']=='miss' and float(ready['estimated_upper_cny'])<1
    first = run(service, ids)
    assert first['status']=='completed' and len(model.calls)==1 and model.purposes==['research_comparison']
    assert len(first['result']['rows'])==2 and set(first['result']['rows'][0]['cells'])==set(FIELDS)
    for row in first['result']['rows']:
        assert all(cell['citations'][0]['document_id']==row['document_id'] for cell in row['cells'].values())
    again=run(service,ids)
    assert again['cache_hit'] and len(model.calls)==1
    assert service.prepare(ids,'Compare methods',False)['estimated_upper_cny']=='0'
    assert service.prepare(ids,'New question',False)['source_modes']=={doc:'analysis_reuse' for doc in ids}
    with research_db.connect() as c:
        for table in ['research_entries','document_relations','relation_candidates']:
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


@pytest.mark.parametrize('fault',['missing_cell','duplicate','invented_quote','empty_evidence','missing_with_value'])
def test_one_bad_cell_rejects_entire_table_without_retry(research_db,fault):
    ids, model, service = setup(research_db)
    raw=json.loads(output());cell=raw['cells'][0]
    if fault=='missing_cell':raw['cells'].pop()
    elif fault=='duplicate':raw['cells'][-1]=cell
    elif fault=='invented_quote':cell['evidence'][0]='invented-id'
    elif fault=='empty_evidence':cell['evidence']=[]
    else:cell['status']='insufficient'
    model.result=json.dumps(raw)
    with pytest.raises(FullTextError):run(service,ids)
    assert len(model.calls)==1
    receipt=run(service,ids)
    assert receipt['status']=='failed' and len(model.calls)==1


def test_new_paper_is_incremental_and_unsupplied_old_quote_cannot_be_guessed(research_db):
    ids, model, service=setup(research_db);run(service,ids)
    third=_seed_document(research_db,content_id='c'*64,display_name='Gamma.pdf',page_texts=[['Gamma background.'],[TEXT[2]]])
    model.result=output(3);run(service,[*ids,third],question='Expanded comparison')
    payload=json.loads(model.calls[-1]);assert any(e['id']=='d3p1s1' for e in payload['source_excerpts'])
    assert not any(e['id']=='d1p1s1' for e in payload['source_excerpts'])
    assert payload['reading_modes']=={'d1':'analysis_reuse','d2':'analysis_reuse','d3':'full_text'}
    raw=json.loads(output(3));raw['cells'][0]['evidence'][0]='d1p3s1'
    model.result=json.dumps(raw)
    with pytest.raises(FullTextError):run(service,[*ids,third],question='Guess an old quote')
    assert len(model.calls)==3


def test_wrong_paper_and_unsupported_numbers_are_empty_not_displayed_as_findings(research_db):
    ids,model,service=setup(research_db);raw=json.loads(output())
    raw['cells'][0]['evidence']=['d2p2s1'];raw['cells'][1]['value']='The study sampled 999 sites.'
    model.result=json.dumps(raw);saved=run(service,ids)
    cells=saved['result']['rows'][0]['cells']
    for field in ['subject','sample']:
        assert cells[field]['status']=='unverified' and cells[field]['value']=='' and not cells[field]['citations']
    assert saved['result']['unverified_cells']==2 and saved['result']['verified_cells']==10
    assert run(service,ids)['cache_hit'] and len(model.calls)==1


def test_excerpt_windows_cover_every_full_page_character_without_retrieval(research_db):
    ids,model,service=setup(research_db)
    plan,_,_=service.plan(ids,'All pages',False)
    excerpts=json.loads(plan.context.prompt)['source_excerpts']
    for ref,page in plan.prepared.pages.items():
        text=' '.join(page.text.split());covered=set()
        for item in excerpts:
            if item['ref']==ref:
                start=text.index(item['text']);covered.update(range(start,start+len(item['text'])))
        assert covered==set(range(len(text)))


def test_annotation_only_content_change_reuses_table_and_rebinds_citations_without_losing_generation_version(research_db):
    ids,model,service=setup(research_db);saved=run(service,ids)
    _seed_document(research_db,content_id='d'*64,display_name='Annotated.pdf',
        page_texts=[['Study background.'],[TEXT[0]],['An unprovided conclusion exists elsewhere on this page.']])
    with research_db.transaction() as c:c.execute('UPDATE documents SET content_sha256=? WHERE id=?',('d'*64,ids[0]))
    ready=service.prepare(ids,'Compare methods',False);assert ready['cache_status']=='hit'
    viewed=service.receipt(saved['request_id']);assert not viewed['source_changed']
    row=viewed['result']['rows'][0];assert row['source_content_id_at_generation']=='a'*64
    assert row['content_id']=='d'*64 and row['cells']['sample']['citations'][0]['content_id']=='d'*64
    assert len(model.calls)==1


def test_corrupt_table_cache_is_blocked_without_paid_fallback(research_db):
    ids,model,service=setup(research_db);saved=run(service,ids)
    with research_db.transaction() as c:
        record=json.loads(c.execute('SELECT checkpoint_json FROM jobs WHERE id=?',(saved['request_id'],)).fetchone()[0])
        record['raw']='{"cells":[]}'
        c.execute('UPDATE jobs SET checkpoint_json=? WHERE id=?',(json.dumps(record),saved['request_id']))
    with pytest.raises(FullTextError,match='comparison_cache_unverified'):service.receipt(saved['request_id'])
    with pytest.raises(FullTextError,match='comparison_cache_unverified'):service.prepare(ids,'Compare methods',False)
    assert len(model.calls)==1


def test_sources_and_plan_changes_block_before_call_and_old_results_are_marked(research_db):
    ids,model,service=setup(research_db);ready=service.prepare(ids,'Compare methods',False)
    SettingsRepository(research_db).update(deepseek_model='deepseek-v4-pro')
    with pytest.raises(FullTextError,match='full_text_plan_changed'):
        service.analyze(str(uuid4()),ids,'Compare methods',False,ready['source_versions'],ready['plan_id'])
    assert not model.calls
    saved=run(service,ids)
    with research_db.transaction() as c:c.execute("UPDATE documents SET status='missing' WHERE id=?",(ids[1],))
    assert service.receipt(saved['request_id'])['source_changed']


def test_inflight_requests_coalesce_and_new_generation_does_not_replay(research_db):
    ids,model,service=setup(research_db);started=Event();release=Event();original=model.answer_question
    class Blocking:
        def answer_question(self,**kwargs):
            started.set();assert release.wait(5);return original(**kwargs)
    service.model=Blocking();ready=service.prepare(ids,'Compare methods',False);rid=str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(service.analyze,rid,ids,'Compare methods',False,ready['source_versions'],ready['plan_id'])
        assert started.wait(5)
        duplicate=service.analyze(str(uuid4()),ids,'Compare methods',False,ready['source_versions'],ready['plan_id'])
        assert duplicate['status']=='running'
        other=Comparison(research_db,RagChunkRepository(research_db),SettingsRepository(research_db),model,'gen2')
        assert other.receipt(rid)['status']=='unknown'
        release.set();assert pending.result()['status']=='completed'
    assert len(model.calls)==1


def test_api_authentication_and_readonly_history(client,authenticated,monkeypatch):
    ids,model,service=setup(client.app.state.database)
    monkeypatch.setattr(client.app.state.research_service,'comparisons',lambda _:service)
    path='/api/workspace/comparisons';body={'document_ids':ids,'question':'Compare methods'}
    assert client.post(path+'/prepare',json=body).status_code==403
    headers={'Origin':'http://testserver','X-LW-CSRF':authenticated.csrf}
    ready=client.post(path+'/prepare',headers=headers,json=body).json()
    result=client.post(path,headers=headers,json={**body,'request_id':str(uuid4()),'source_versions':ready['source_versions'],'plan_id':ready['plan_id']})
    assert result.status_code==200,result.text
    assert client.get(path).json()['items'][0]['document_ids']==ids
    assert client.get(path+'/'+result.json()['request_id']).json()['result']['rows']
    assert len(model.calls)==1
