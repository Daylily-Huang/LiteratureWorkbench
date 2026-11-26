"""Carry paid study evidence into human review without invoking a model."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from collections.abc import Mapping

from literature_workbench.db.database import Database
from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.study_library import StudyLibrary
from literature_workbench.features.settings.repository import SettingsRepository


def study_context_changed(proposal: dict[str, Any], docs: Mapping[str, Any]) -> bool:
    versions = (proposal.get('study_origin') or proposal.get('reading_origin') or {}).get('source_versions', {})
    return any(doc not in docs or docs[doc]['content_sha256'] != version['content_id']
               or docs[doc]['current_parse_run_id'] != version['parse_run_id']
               for doc, version in versions.items())


def draft_from_study(database: Database, cache_key: str, claim_index: int,
                     source_index: int, target_index: int) -> dict[str, Any]:
    # Serialize validation and insertion against parser/import/cache writers.
    with database.transaction() as c:
        row = c.execute('SELECT * FROM full_text_study_cache WHERE cache_key=?', (cache_key,)).fetchone()
        if row is None:
            raise FullTextError('study_not_found')
        library = StudyLibrary(database, RagChunkRepository(database), SettingsRepository(database).read().deepseek_model)
        entry = library.inspect(row)
        if entry['invalid'] or not entry['current']:
            raise FullTextError('study_evidence_changed')
        try:
            claim = entry['claims'][claim_index]
            source = claim['evidence'][source_index]
            target = claim['evidence'][target_index]
        except IndexError:
            raise FullTextError('study_evidence_selection_invalid') from None
        if source['document_id'] == target['document_id']:
            raise FullTextError('study_two_documents_required')
        origin = {'cache_key': cache_key, 'claim_index': claim_index, 'source_index': source_index,
                  'target_index': target_index, 'created_at': entry['created_at'],
                  'model': entry['model'], 'context_kind': entry['context_kind'],
                  'documents': entry['documents'], 'conclusion': claim['conclusion'],
                  'evidence': claim['evidence'],
                  'limitations': entry['limitations'],
                  'source_versions': {doc: {key: p.coverage[0][key] for key in ('content_id', 'parse_run_id')}
                                      for doc, p in library.prepared.items() if p}}
        # The original order/claim and exact chosen direction define an import.
        prior = c.execute("""SELECT analysis_id,id,proposal_json FROM relation_candidates
            WHERE json_extract(proposal_json,'$.study_origin.cache_key')=?
              AND json_extract(proposal_json,'$.study_origin.claim_index')=?
              AND json_extract(proposal_json,'$.study_origin.source_index')=?
              AND json_extract(proposal_json,'$.study_origin.target_index')=?""",
            (cache_key, claim_index, source_index, target_index)).fetchall()
        existing = next((r for r in prior if json.loads(r['proposal_json']).get('study_origin') == origin), None)
        ids = [d['document_id'] for d in entry['documents']]
        if existing:
            return {'id': existing['analysis_id'], 'candidate_id': existing['id'],
                    'document_ids': ids, 'cache_hit': True, 'api_call_required': False}
        analysis_id, candidate_id = str(uuid4()), str(uuid4())
        warning = '此为已有精读结论的人工关系草稿，没有调用 AI；共同引用不等于支持或矛盾，请核对两侧及原组语境后选择关系。'
        proposal = {'source': source, 'target': target, 'relation': 'related',
                    'difference_basis': 'unspecified', 'rationale': claim['conclusion'],
                    'comparison_note': '待核对：方法、样本、测量结果与适用条件；尚未判断支持、矛盾或方法差异。',
                    'caveats': [warning, *entry['limitations']], 'study_origin': origin}
        coverage = [{**doc, 'retrieved': True, 'has_candidate': doc['document_id'] in
                     (source['document_id'], target['document_id']), 'reading_mode': 'study_cache'}
                    for doc in entry['documents']]
        c.execute('INSERT INTO relation_analyses VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (analysis_id, entry['question'], json.dumps(ids), 'study_cache', entry['model'], None,
                   claim['conclusion'], json.dumps([warning], ensure_ascii=False),
                   json.dumps(coverage, ensure_ascii=False), datetime.now(UTC).isoformat()))
        c.execute('INSERT INTO relation_candidates(id,analysis_id,proposal_json) VALUES(?,?,?)',
                  (candidate_id, analysis_id, json.dumps(proposal, ensure_ascii=False)))
        return {'id': analysis_id, 'candidate_id': candidate_id, 'document_ids': ids,
                'cache_hit': False, 'api_call_required': False}
