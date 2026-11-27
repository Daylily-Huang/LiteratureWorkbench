"""Durable inline analysis attempts; these records are never worker-replayed."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from literature_workbench.db.database import Database
from literature_workbench.features.research.relation_analysis import RELATION_INSTRUCTION
from literature_workbench.features.settings.repository import SettingsRepository

KIND = "relation_analysis_attempt"


def settings_snapshot(database: Database) -> dict[str, Any]:
    settings = asdict(SettingsRepository(database).read())
    settings.pop('revision')
    return settings


class AttemptError(RuntimeError):
    pass


class RelationRequests:
    def __init__(self, database: Database, generation: str) -> None:
        self.database = database
        self.generation = generation

    def sources(self, connection: sqlite3.Connection, ids: list[str]) -> dict[str, list[str | None]]:
        return {str(r['id']): [str(r['content_sha256']), r['current_parse_run_id']] for r in connection.execute(
            "SELECT d.id,d.content_sha256,c.current_parse_run_id FROM documents d JOIN contents c ON c.sha256=d.content_sha256 "
            "JOIN libraries l ON l.id=d.library_id WHERE l.status='active' AND d.status IN ('candidate_pdf','valid_pdf') "
            "AND d.format='pdf' AND d.id IN (" + ','.join('?' for _ in ids) + ')', ids)}

    def reserve(self, request_id: str, question: str, ids: list[str], force: bool, *,
                reading_mode: str = 'retrieval', plan_id: str | None = None,
                source_versions: dict[str, str] | None = None) -> tuple[bool, dict[str, Any]]:
        binding = {'question': question, 'document_ids': list(ids), 'force_refresh': force}
        instruction = RELATION_INSTRUCTION
        if reading_mode == 'full_text':
            from literature_workbench.features.research.full_text_relations import RELATION_SYSTEM
            binding.update(reading_mode=reading_mode, plan_id=plan_id, source_versions=source_versions)
            instruction = RELATION_SYSTEM
        settings = settings_snapshot(self.database)
        with self.database.transaction() as c:
            existing = c.execute('SELECT * FROM jobs WHERE id=?', (request_id,)).fetchone()
            if existing is not None:
                if existing['kind'] != KIND or json.loads(existing['payload_json']).get('binding') != binding:
                    raise AttemptError('analysis_request_conflict')
                return False, self._status(c, existing, reused=True)
            sources = self.sources(c, ids)
            if len(sources) != len(ids):
                raise AttemptError('document_not_found')
            dedup_binding = {k: v for k, v in binding.items() if k not in {'plan_id', 'source_versions'}}
            fingerprint = hashlib.sha256(json.dumps({'version': 1, 'binding': {**dedup_binding, 'force_refresh': False},
                'sources': sources, 'settings': settings, 'instruction': instruction}, sort_keys=True).encode()).hexdigest()
            dedup = 'relation-analysis:' + fingerprint
            active = c.execute("SELECT * FROM jobs WHERE kind=? AND dedup_key=? AND status='running'", (KIND, dedup)).fetchone()
            if active is not None and json.loads(active['payload_json']).get('generation') != self.generation and force:
                # Explicit new attempt after a process interruption; never silently replay the old one.
                c.execute("UPDATE jobs SET status='failed',stage='uncertain',error_code='analysis_interrupted',updated_at=? WHERE id=?",
                          (datetime.now(UTC).isoformat(), active['id']))
                active = None
            cached = None if force or active is not None else c.execute(
                "SELECT j.* FROM jobs j JOIN relation_analyses a ON a.id=json_extract(j.checkpoint_json,'$.analysis_id') "
                "WHERE j.kind=? AND j.dedup_key=? AND j.status='completed' AND j.stage='complete' ORDER BY j.updated_at DESC,j.id DESC LIMIT 1",
                (KIND, dedup)).fetchone()
            failed = None if force or active is not None or cached is not None else c.execute(
                "SELECT * FROM jobs WHERE kind=? AND dedup_key=? AND status='failed' "
                "AND (error_code IS NULL OR error_code <> 'full_text_plan_changed') ORDER BY updated_at DESC,id DESC LIMIT 1",
                (KIND, dedup)).fetchone()
            root = active if active is not None else cached if cached is not None else failed
            now = datetime.now(UTC).isoformat()
            payload = json.dumps({'binding': binding, 'sources': sources, 'generation': self.generation, 'settings': settings})
            c.execute("INSERT INTO jobs(id,kind,dedup_key,payload_json,status,stage,checkpoint_json,attempts,retryable,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (request_id, KIND, 'relation-reference:' + request_id if root is not None else dedup, payload,
                       'completed' if root is not None else 'running', 'reference' if root is not None else 'api_request',
                       json.dumps({'canonical_request_id': root['id']}) if root is not None else '{}', 0 if root is not None else 1, 0, now, now))
            row = c.execute('SELECT * FROM jobs WHERE id=?', (request_id,)).fetchone()
            return root is None, self._status(c, row, reused=root is not None)

    def _status(self, c: sqlite3.Connection, row: sqlite3.Row, *, reused: bool) -> dict[str, Any]:
        request_id = row['id']
        saved = json.loads(row['payload_json'])
        canonical = json.loads(row['checkpoint_json']).get('canonical_request_id')
        if canonical:
            parent = c.execute('SELECT * FROM jobs WHERE id=? AND kind=?', (canonical, KIND)).fetchone()
            if parent is None or parent['stage'] == 'reference':
                return {'request_id': request_id, 'status': 'unknown', 'cache_hit': False}
            row = parent
        checkpoint = json.loads(row['checkpoint_json'])
        status = row['status']
        if status == 'running' and json.loads(row['payload_json']).get('generation') != self.generation:
            status = 'unknown'
        if status not in {'running', 'completed', 'failed'}:
            status = 'unknown'
        result = {'request_id': request_id, 'status': status, 'cache_hit': reused, 'error_code': row['error_code'],
                  'question': saved['binding']['question'], 'document_ids': saved['binding']['document_ids'], 'created_at': row['created_at']}
        if status == 'completed':
            analysis = c.execute('SELECT id FROM relation_analyses WHERE id=?', (checkpoint.get('analysis_id'),)).fetchone()
            if analysis is None:
                return {**result, 'status': 'unknown'}
            current = self.sources(c, saved['binding']['document_ids'])
            result.update(id=analysis['id'], candidate_count=c.execute('SELECT count(*) FROM relation_candidates WHERE analysis_id=?', (analysis['id'],)).fetchone()[0],
                          source_changed=current != saved['sources'])
        return result

    def status(self, request_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=False) as c:
            row = c.execute('SELECT * FROM jobs WHERE id=? AND kind=?', (request_id, KIND)).fetchone()
            if row is None:
                raise AttemptError('analysis_request_not_found')
            return {**self._status(c, row, reused=True), 'readonly': True}

    def assert_current(self, connection: sqlite3.Connection, request_id: str) -> None:
        row = connection.execute('SELECT payload_json FROM jobs WHERE id=? AND kind=?', (request_id, KIND)).fetchone()
        if row is None:
            raise AttemptError('analysis_request_not_found')
        saved = json.loads(row['payload_json'])
        if self.sources(connection, saved['binding']['document_ids']) != saved['sources']:
            raise AttemptError('analysis_source_changed')
        if settings_snapshot(self.database) != saved['settings']:
            raise AttemptError('analysis_settings_changed')

    def complete(self, connection: sqlite3.Connection, request_id: str, analysis_id: str) -> None:
        connection.execute("UPDATE jobs SET status='completed',stage='complete',checkpoint_json=?,updated_at=? WHERE id=? AND kind=? AND status='running'",
                           (json.dumps({'analysis_id': analysis_id}), datetime.now(UTC).isoformat(), request_id, KIND))

    def fail(self, request_id: str, code: str) -> None:
        with self.database.transaction() as c:
            c.execute("UPDATE jobs SET status='failed',stage='failed',error_code=?,updated_at=? WHERE id=? AND kind=? AND status='running'",
                      (code, datetime.now(UTC).isoformat(), request_id, KIND))
