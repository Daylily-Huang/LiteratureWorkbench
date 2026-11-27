"""Read-only discovery of paid studies; browsing never invokes a model."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from literature_workbench.db.database import Database
from literature_workbench.features.research.full_text import FullTextError, FullTextStudy, PreparedStudy, parse_output
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.study_cache import StudyCache, digest


class StudyLibrary:
    def __init__(self, database: Database, repository: RagChunkRepository, model: str) -> None:
        self.database = database
        self.study = FullTextStudy(database, repository)
        self.model = model
        self.prepared: dict[str, PreparedStudy | None] = {}
        with database.connect() as c:
            self.names = {str(r[0]): str(r[1]) for r in c.execute("""SELECT d.id,d.display_name
                FROM documents d JOIN libraries l ON l.id=d.library_id AND l.status='active'
                WHERE d.status IN ('candidate_pdf','valid_pdf') AND d.format='pdf'""")}

    def _prepare(self, doc: str) -> PreparedStudy | None:
        if doc not in self.prepared:
            try:
                self.prepared[doc] = self.study.prepare([doc], "缓存原文核对", self.model, check_budget=False)
            except FullTextError:
                self.prepared[doc] = None
        return self.prepared[doc]

    def _rows(self, doc: str, *, offset: int = 0, limit: int = 21, witnesses_first: bool = False) -> tuple[int, list[sqlite3.Row]]:
        where = """EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.document_order_json)
                   THEN c.document_order_json ELSE '[]' END) j WHERE j.value=?)"""
        order = "c.created_at DESC,c.cache_key"
        if witnesses_first:
            order = """(c.context_kind='full_text' OR EXISTS(SELECT 1 FROM full_text_study_documents m
                        WHERE m.cache_key=c.cache_key AND m.document_id=? AND m.read_mode='full_text')) DESC,""" + order
        with self.database.connect() as c:
            total = int(c.execute(f"SELECT count(*) FROM full_text_study_cache c WHERE {where}", (doc,)).fetchone()[0])
            rows = c.execute(f"SELECT c.* FROM full_text_study_cache c WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                             [doc, *([doc] if witnesses_first else []), limit, offset]).fetchall()
        return total, rows

    def inspect(self, row: sqlite3.Row) -> dict[str, Any]:
        entry: dict[str, Any] = {"cache_key": row['cache_key'], "question": row['question'], "model": row['model'],
            "created_at": row['created_at'], "context_kind": row['context_kind'], "documents": [],
            "claims": [], "limitations": [], "current": False, "invalid": True}
        try:
            order = json.loads(row['document_order_json'])
            if not isinstance(order, list) or not 1 <= len(order) <= 8 or any(not isinstance(d, str) for d in order) or len(set(order)) != len(order):
                return entry
            output = parse_output(row['output_json'])
            if any(not cl.conclusion.strip() or re.search(r'\[d\d+', cl.conclusion) for cl in output.claims) or any(not text.strip() or len(text) > 2000 for text in output.limitations):
                return entry
            with self.database.connect() as c:
                members = {str(r[0]): (str(r[1]), str(r[2])) for r in c.execute(
                    "SELECT document_id,text_fingerprint,read_mode FROM full_text_study_documents WHERE cache_key=?", (row['cache_key'],))}
            preparations = {doc: self._prepare(doc) for doc in order if doc in self.names}
            if not members and len(preparations) == len(order) and all(preparations.values()):
                fingerprints = {doc: p.fingerprints[doc] for doc, p in preparations.items() if p}
                if digest([fingerprints, row['model'], row['prompt_version']]) == row['scope_key']:
                    members = {doc: (fingerprints[doc], row['context_kind']) for doc in order}
            if members and (set(members) != set(order) or digest([{doc: members[doc][0] for doc in order}, row['model'], row['prompt_version']]) != row['scope_key']):
                return entry
            states: dict[str, str] = {}
            for doc in order:
                p = preparations.get(doc)
                state = 'unavailable' if p is None else 'unverified' if doc not in members else 'current' if p.fingerprints[doc] == members[doc][0] else 'changed'
                states[doc] = state
                entry['documents'].append({'document_id': doc, 'display_name': self.names.get(doc, '已不可用的文献'),
                    'text_status': state, 'read_mode': members.get(doc, ('', row['context_kind']))[1]})
            for claim in output.claims:
                evidence: list[dict[str, Any]] = []
                for quote in claim.evidence:
                    number, page = quote.ref[1:-1].split(' ')
                    index = int(number[1:]) - 1
                    if not 0 <= index < len(order):
                        return entry
                    doc = order[index]
                    physical_page = int(page[1:])
                    p = preparations.get(doc)
                    source = p.pages.get(f'[d1 p{physical_page}]') if p else None
                    normalized = ' '.join(quote.quote.split())
                    verified = states[doc] == 'current' and source is not None and len(normalized) >= 8 and normalized in ' '.join(source.text.split())
                    if states[doc] == 'current' and not verified:
                        return {**entry, 'claims': [], 'limitations': [], 'invalid': True}
                    evidence.append({'document_id': doc, 'display_name': self.names.get(doc, '已不可用的文献'),
                        'physical_page': physical_page, 'excerpt': quote.quote, 'verified': verified,
                        'content_id': source.content_id if source and verified else None, 'rects': []})
                entry['claims'].append({'conclusion': claim.conclusion, 'evidence': evidence})
            entry['limitations'] = output.limitations
            entry['invalid'] = False
            entry['current'] = all(s == 'current' for s in states.values()) and all(e['verified'] for cl in entry['claims'] for e in cl['evidence'])
            entry['rules_compatible'] = row['prompt_version'] in StudyCache._compatible_versions()
            # A parse/identity change during reading invalidates the whole snapshot.
            for p in preparations.values():
                if p:
                    self.study.assert_current(p)
            return entry
        except (FullTextError, ValueError, TypeError, KeyError, IndexError):
            return {**entry, 'claims': [], 'limitations': [], 'current': False, 'invalid': True}

    def summaries(self) -> dict[str, object]:
        items: list[dict[str, object]] = []
        with self.database.connect() as c:
            candidates = {str(r[0]) for r in c.execute("""SELECT DISTINCT j.value FROM full_text_study_cache c,
                json_each(CASE WHEN json_valid(c.document_order_json) THEN c.document_order_json ELSE '[]' END) j""")}
        for doc in self.names:
            if doc not in candidates:
                continue
            total, rows = self._rows(doc, limit=64, witnesses_first=True)
            if not total:
                continue
            entries = [self.inspect(r) for r in rows]
            witnessed = []
            changed = False
            for entry in entries:
                if entry['invalid']:
                    continue
                member = next(d for d in entry['documents'] if d['document_id'] == doc)
                changed = changed or member['text_status'] == 'changed'
                if member['read_mode'] == 'full_text' and member['text_status'] == 'current' and any(
                    e['document_id'] == doc and e['verified'] for cl in entry['claims'] for e in cl['evidence']
                ):
                    witnessed.append(entry)
            ready = self._prepare(doc) is not None
            status = 'studied' if witnessed else 'unavailable' if not ready else 'stale' if changed else 'history'
            items.append({'document_id': doc, 'status': status, 'history_count': total,
                'last_studied_at': witnessed[0]['created_at'] if witnessed else None,
                'reusable': any(e['model'] == self.model and e.get('rules_compatible') for e in witnessed)})
        for p in self.prepared.values():
            if p:
                self.study.assert_current(p)
        return {'items': items}

    def history(self, doc: str, offset: int = 0) -> dict[str, object]:
        if doc not in self.names:
            raise FullTextError('document_not_found')
        total, rows = self._rows(doc, offset=offset)
        return {'document_id': doc, 'display_name': self.names[doc], 'total': total, 'offset': offset,
            'has_more': len(rows) > 20, 'items': [self.inspect(r) for r in rows[:20]], 'api_call_required': False}
