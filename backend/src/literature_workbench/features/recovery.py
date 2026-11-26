"""Prepare a self-contained workspace using only verified, freshly extracted files."""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.backups import BackupError, file_hash, io_path, now, plain_path
from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.features.reader.pdf_stream import load_registered_pdf, open_verified_pdf
from literature_workbench.platform.windows.filesystem import SafeFileInspector
from literature_workbench.platform.windows.paths import ordinary_path

MARKER = 'recovered-workspace.json'


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(path: Path, root: Path) -> Path:
    checked = plain_path(path)
    if not checked.is_relative_to(root) or checked == root:
        raise BackupError('recovery_invalid_workspace')
    return checked


def prepare_workspace(destination: Path, inspector: SafeFileInspector, checkpoint: Callable[[], None]) -> dict[str, Any]:
    """Only called after archive hash, all member hashes and DB coverage checks pass."""
    destination = plain_path(destination)
    manifest = json.loads((destination/'manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('include_pdfs'):
        raise BackupError('recovery_full_backup_required')
    data = plain_path(destination/'data')
    original = Path(manifest['data_root'])
    database = Database(data/'db/literature-workbench.sqlite3')
    stamp = now()
    saved = {d['document_id']: d for d in manifest['documents']}
    remapped: dict[str, Path] = {}

    def rebase(value: str) -> Path:
        try:
            return _inside(data/ordinary_path(Path(value)).relative_to(original), data)
        except ValueError:
            raise BackupError('recovery_invalid_workspace') from None

    with database.transaction() as c:
        roots: dict[str, LibraryRoot] = {}
        library_rows = c.execute('SELECT * FROM libraries').fetchall()
        original_roots = {str(row['id']): Path(row['root_path']) for row in library_rows}
        for row in library_rows:
            checkpoint()
            library_id = str(UUID(row['id']))
            old_root = Path(row['root_path'])
            target = data/'imports/library' if row['discovery_mode'] == 'managed_import' else data/'recovered-pdfs'/library_id/(old_root.parent.name or '恢复文献')/(old_root.name or '文献库')
            target = _inside(target, data)
            io_path(target).mkdir(parents=True, exist_ok=True)
            identity = inspector.inspect_root_directory(target)
            roots[library_id] = LibraryRoot(UUID(library_id), identity.canonical_path, identity.volume_id,
                identity.file_id, bool(row['recursive']), row['discovery_mode'], row['authorization_source'], row['status'])
            remapped[_key(Path(row['root_path']))] = target
            c.execute('UPDATE libraries SET root_path=?,root_path_key=?,root_volume_id=?,root_file_id=?,updated_at=? WHERE id=?',
                (str(target), _key(target), identity.volume_id, identity.file_id, stamp, library_id))
        for row in c.execute('SELECT * FROM documents').fetchall():
            checkpoint()
            document_id = str(UUID(row['id']))
            root = roots[row['library_id']]
            record = saved.get(document_id)
            if record is None:
                # Unavailable/inactive sources were explicitly excluded from the archive.
                target = _inside(data/'unavailable'/str(root.id)/f'{document_id}.pdf', data)
                c.execute('UPDATE documents SET path=?,path_key=?,status=? WHERE id=?',
                    (str(target), _key(target), 'missing' if root.status == 'active' else row['status'], document_id))
                continue
            try:
                relative = Path(row['path']).relative_to(original_roots[str(root.id)])
            except ValueError:
                raise BackupError('recovery_invalid_workspace') from None
            target = _inside(root.canonical_path/relative, root.canonical_path)
            source = _inside(destination/record['archive_path'], destination)
            io_path(target).parent.mkdir(parents=True, exist_ok=True)
            if source != target:
                if io_path(target).exists():
                    if file_hash(target) != record['sha256']:
                        raise BackupError('backup_hash_mismatch')
                else:
                    # Both endpoints are inside this new recovery candidate.
                    io_path(source).replace(io_path(target))
            observed = inspector.inspect_under_root(target, root)
            if observed.is_reparse_point or observed.hard_link_count != 1 or file_hash(target) != record['sha256']:
                raise BackupError('backup_hash_mismatch')
            remapped[_key(Path(row['path']))] = target
            c.execute('''UPDATE documents SET path=?,path_key=?,size_bytes=?,mtime_ns=?,volume_id=?,file_id=?,
                hard_link_count=?,updated_at=? WHERE id=?''', (str(target), _key(target), observed.size,
                observed.modified_ns, observed.volume_id, observed.file_id, observed.hard_link_count, stamp, document_id))
            c.execute('''INSERT INTO document_versions(id,document_id,content_sha256,size_bytes,mtime_ns,volume_id,
                file_id,hard_link_count,observed_at) VALUES(?,?,?,?,?,?,?,?,?)''', (str(uuid4()), document_id,
                row['content_sha256'], observed.size, observed.modified_ns, observed.volume_id, observed.file_id,
                observed.hard_link_count, stamp))
        for table in ('pdf_annotation_baselines', 'pdf_annotation_mutations'):
            expected_column = 'content_sha256' if table.endswith('baselines') else 'pre_content_sha256'
            for row in c.execute(f'SELECT rowid,* FROM {table}').fetchall():
                path = rebase(row['backup_path'])
                if file_hash(path) != row[expected_column]:
                    raise BackupError('backup_recovery_missing')
                c.execute(f'UPDATE {table} SET backup_path=? WHERE rowid=?', (str(path), row['rowid']))
        for row in c.execute('SELECT id,output_dir FROM parse_runs WHERE output_dir IS NOT NULL').fetchall():
            value = Path(row['output_dir'])
            target = rebase(row['output_dir']) if value.is_absolute() else _inside(data/value, data)
            relative_dir = target.relative_to(data).as_posix()
            c.execute('UPDATE parse_runs SET output_dir=? WHERE id=?', (relative_dir if io_path(target).is_dir() else None, row['id']))
        for table in ('confirmed_imports', 'import_intents'):
            for row in c.execute(f'SELECT rowid,final_path_key FROM {table} WHERE final_path_key IS NOT NULL').fetchall():
                target = remapped.get(_key(Path(row['final_path_key']))) or rebase(row['final_path_key'])
                c.execute(f'UPDATE {table} SET final_path_key=? WHERE rowid=?', (_key(target), row['rowid']))
        # Leases, upload capabilities and cleanup ownership belong to the old process/files.
        c.execute("UPDATE jobs SET status='failed',stage='failed',error_code='workspace_restored',retryable=0,lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE status IN ('queued','running')", (stamp,))
        c.execute("UPDATE translation_tasks SET status='expired',stage='expired',error_code='session_expired',updated_at=? WHERE status IN ('queued','running','pending')", (stamp,))
        c.execute("UPDATE model_validation_tasks SET status='failed',stage='failed',error_code='workspace_restored',updated_at=? WHERE status NOT IN ('completed','failed','expired','cancelled')", (stamp,))
        c.execute("UPDATE import_intents SET status='failed',upload_token_hash=NULL,error_code='workspace_restored',updated_at=? WHERE status IN ('pending','receiving','uploaded','validating','ready_to_promote')", (stamp,))
        c.execute('DELETE FROM import_promotions')
        c.execute('UPDATE one_time_egress_grants SET revoked_at=? WHERE revoked_at IS NULL', (stamp,))
        c.execute("UPDATE parse_runs SET status='failed',error_code='workspace_restored',updated_at=?,completed_at=? WHERE status IN ('queued','running')", (stamp, stamp))
    with database.connect() as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchone():
            raise BackupError('backup_database_invalid')
    for document_id in saved:
        registered = load_registered_pdf(database, UUID(document_id))
        with open_verified_pdf(inspector, registered).stream:
            pass
    result = {'format': 'literature-workbench-recovery', 'version': 1, 'ready': True,
        'workspace_root': str(data), 'created_at': stamp, 'pdf_count': len(saved),
        'source_created_at': manifest['created_at'], 'counts': manifest['counts'],
        'restored_libraries': list(roots),
        'unavailable_documents': max(0, manifest['counts']['documents'] - len(saved)),
        'note': '独立恢复工作台；文献与批注使用此副本，原库未替换。API密钥与模型需在当前机器配置，未完成的旧任务不会重放。'}
    (destination/MARKER).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (destination/'恢复说明.txt').write_text(result['note'], encoding='utf-8')
    return {'workspace_root': str(data), 'recovered_pdfs': len(saved), 'recovery_ready': True,
            'unavailable_documents': result['unavailable_documents']}


def validate_workspace(data: Path) -> Path:
    """Reject incomplete/relocated candidates before launching any worker or writable DB."""
    data = plain_path(data)
    try:
        marker = json.loads((data.parent/MARKER).read_text(encoding='utf-8'))
        if marker.get('format') != 'literature-workbench-recovery' or marker.get('version') != 1 or marker.get('ready') is not True or _key(Path(marker['workspace_root'])) != _key(data):
            raise BackupError('recovery_invalid_workspace')
        database_path = _inside(data/'db/literature-workbench.sqlite3', data)
        with sqlite3.connect(f'{database_path.as_uri()}?mode=ro', uri=True) as c:
            if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or c.execute('PRAGMA foreign_key_check').fetchone():
                raise BackupError('backup_database_invalid')
            restored = marker['restored_libraries']
            if not isinstance(restored, list) or any(not isinstance(i, str) for i in restored):
                raise BackupError('recovery_invalid_workspace')
            for identifier, value in c.execute('SELECT id,root_path FROM libraries'):
                if identifier in restored:
                    _inside(Path(value), data)
            for library_id, value in c.execute('SELECT library_id,path FROM documents'):
                if library_id in restored:
                    _inside(Path(value), data)
            # New linked folders explicitly added through the normal picker retain its grants.
            for (value,) in c.execute('SELECT output_dir FROM parse_runs WHERE output_dir IS NOT NULL'):
                if Path(value).is_absolute():
                    raise BackupError('recovery_invalid_workspace')
                _inside(data/value, data)
            for table, column in [('pdf_annotation_baselines','backup_path'), ('pdf_annotation_mutations','backup_path'), ('confirmed_imports','final_path_key'), ('import_intents','final_path_key')]:
                for row in c.execute(f'SELECT {column} FROM {table} WHERE {column} IS NOT NULL'):
                    _inside(Path(row[0]), data)
        return data
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        raise BackupError('recovery_invalid_workspace') from None
