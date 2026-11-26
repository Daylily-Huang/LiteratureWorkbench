"""Local, verifiable workspace snapshots. Never overwrites a live library."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile
from contextlib import closing, nullcontext
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.reader.pdf_stream import (
    DocumentIdentityChanged, DocumentNotFound, DocumentNotReadable, load_registered_pdf, open_verified_pdf,
)
from literature_workbench.platform.windows.filesystem import SafeFileInspector
from literature_workbench.platform.windows.paths import io_path as io_path, ordinary_path

CHUNK = 1024 * 1024
MAX_ENTRIES = 200_000
MAX_BYTES = 100 * 1024**3
MAX_UPLOAD_BYTES = MAX_BYTES + 512 * 1024**2
DATA_TABLES = ('documents', 'reading_records', 'research_entries', 'reading_groups',
               'document_relations', 'relation_candidates', 'full_text_study_cache',
               'full_text_study_documents', 'translation_cache', 'pdf_annotation_mutations')
LOGGER = logging.getLogger(__name__)


class BackupError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def file_hash(path: Path, checkpoint: Callable[[], None] | None = None) -> str:
    with io_path(path).open('rb') as source:
        result = hashlib.sha256()
        while chunk := source.read(CHUNK):
            if checkpoint:
                checkpoint()
            result.update(chunk)
        return result.hexdigest()


def plain_path(path: Path) -> Path:
    # Reject junctions/symlinks at every existing component, including the root.
    absolute = ordinary_path(path)
    for part in [*reversed(absolute.parents), absolute]:
        checked = io_path(part)
        if checked.exists() or checked.is_symlink():
            info = checked.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise BackupError('backup_unsafe_path')
    return absolute


def workspace_files(base: Path) -> Iterator[Path]:
    def failed(_: OSError) -> None:
        raise BackupError('backup_io_error')
    for current, directories, filenames in os.walk(io_path(base), followlinks=False, onerror=failed):
        value = str(current)
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
        parent = plain_path(Path(value))
        for directory in directories:
            plain_path(parent / directory)
        for filename in sorted(filenames):
            yield plain_path(parent / filename)


def recovery_files(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(str(r[0]), str(r[1])) for r in connection.execute('''SELECT backup_path,content_sha256 FROM pdf_annotation_baselines
        UNION SELECT backup_path,pre_content_sha256 FROM pdf_annotation_mutations''')]


class BackupService:
    def __init__(self, database: Database, data_root: Path, inspector: SafeFileInspector) -> None:
        self.database = database
        self.data_root = plain_path(data_root)
        self.root = plain_path(self.data_root / 'backups' / 'workspace')
        self.root.mkdir(parents=True, exist_ok=True)
        self.operations = self.root / 'operations'
        plain_path(self.operations).mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._uploading: str | None = None
        self._thread: threading.Thread | None = None
        self._active: str | None = None
        self.inspector = inspector
        for path in self.operations.glob('*.json'):
            item = self._read(path.stem)
            if item and item.get('status') in {'queued', 'running', 'uploading'}:
                self._update(path.stem, status='failed', error='backup_interrupted')
                if item.get('kind') == 'import':
                    io_path(plain_path(self.root/f'{UUID(path.stem)}.part')).unlink(missing_ok=True)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _read(self, operation: str) -> dict[str, Any] | None:
        try:
            identifier = str(UUID(operation))
        except ValueError:
            return None
        path = plain_path(self.operations / f'{identifier}.json')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) and value.get('id') == operation else None
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
            return None

    def _update(self, operation: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            value = self._read(operation) or {'id': operation, 'created_at': now()}
            value.update(changes, updated_at=now())
            target = plain_path(self.operations / f'{UUID(operation)}.json')
            temporary = plain_path(target.with_suffix('.tmp'))
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            return value

    def job(self, operation: str) -> dict[str, Any]:
        with self._lock:
            result = self._read(operation)
            if result is None:
                raise BackupError('backup_not_found')
            return result

    def overview(self) -> dict[str, Any]:
        with self.database.connect() as c:
            counts = {table: c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in DATA_TABLES}
            linked = c.execute("SELECT count(*) FROM documents d JOIN libraries l ON l.id=d.library_id WHERE l.status='active' AND d.status IN ('valid_pdf','candidate_pdf') AND d.format='pdf'").fetchone()[0]
        with self._lock:
            entries = [value for p in self.operations.glob('*.json') if (value := self._read(p.stem)) and value.get('kind') in {'create', 'import'} and not value.get('duplicate_of')]
            entries.sort(key=lambda item: str(item['created_at']), reverse=True)
            for item in entries:
                item['archive_available'] = io_path(self._archive(item['id'])).is_file() if item.get('status') == 'completed' else False
            active = self._read(self._active) if self._active else None
        recoveries = [value for p in self.operations.glob('*.json') if (value := self._read(p.stem)) and value.get('kind') == 'recover_workspace' and value.get('status') == 'completed']
        recoveries.sort(key=lambda item: str(item['created_at']), reverse=True)
        return {'data_root': str(self.data_root), 'backup_directory': str(self.root),
                'counts': counts, 'active_pdf_count': linked, 'backups': entries[:50],
                'recoveries': recoveries[:20],
                'backups_total': len(entries), 'active_job': active if active and active.get('status') in {'queued','running','uploading'} else None}

    def _archive(self, identifier: str) -> Path:
        return plain_path(self.root / f'{UUID(identifier)}.lwb.zip')

    def download(self, identifier: str) -> Path:
        item = self.job(identifier)
        archive_id = item.get('duplicate_of', identifier)
        source = self.job(archive_id)
        path = self._archive(archive_id)
        if item.get('kind') not in {'create','import'} or item.get('status') != 'completed' or source.get('status') != 'completed' or source.get('archive_sha256') != item.get('archive_sha256') or not io_path(path).is_file():
            raise BackupError('backup_not_found')
        return path

    def start(self, *, include_pdfs: bool = True, kind: str = 'create', backup_id: str | None = None) -> dict[str, Any]:
        if kind not in {'create', 'verify', 'restore_preview', 'recover_workspace'}:
            raise BackupError('backup_invalid_operation')
        with self._lock:
            if self._uploading or (self._thread and self._thread.is_alive()):
                raise BackupError('backup_busy')
            if kind != 'create':
                if backup_id is None:
                    raise BackupError('backup_not_found')
                self.download(backup_id)
            operation = str(uuid4())
            result = self._update(operation, kind=kind, backup_id=backup_id, status='queued',
                include_pdfs=include_pdfs, stage='queued', processed_files=0, error=None)
            self._active = operation
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, args=(operation,), daemon=True, name='workspace-backup')
            self._thread.start()
            return result

    def _cancelled(self) -> None:
        if self._stop.is_set():
            raise BackupError('backup_interrupted')
        if self._cancel.is_set():
            raise BackupError('backup_cancelled')

    def begin_import(self, name: str, size: int) -> dict[str, Any]:
        if not name.casefold().endswith('.lwb.zip') or len(name) > 220 or any(ord(c) < 32 or c in '/\\' for c in name):
            raise BackupError('backup_file_type_invalid')
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise BackupError('backup_too_large')
        with self._lock:
            if self._uploading or (self._thread and self._thread.is_alive()):
                raise BackupError('backup_busy')
            operation = str(uuid4())
            result = self._update(operation, kind='import', status='uploading', stage='uploading',
                source_name=name, expected_bytes=size, received_bytes=0, processed_files=0, error=None)
            self._active = self._uploading = operation
            self._cancel.clear()
            return result

    def import_path(self, operation: str) -> Path:
        if self._uploading != operation:
            raise BackupError('backup_not_found')
        return plain_path(self.root/f'{UUID(operation)}.part')

    def finish_import(self, operation: str, digest: str, size: int) -> dict[str, Any]:
        with self._lock:
            self._cancelled()
            if self._uploading != operation or size != self.job(operation)['expected_bytes']:
                raise BackupError('backup_upload_incomplete')
            result = self._update(operation, status='queued', stage='queued', archive_sha256=digest,
                size_bytes=size, received_bytes=size)
            self._uploading = None
            self._thread = threading.Thread(target=self._run, args=(operation,), daemon=True, name='backup-import')
            self._thread.start()
            return result

    def fail_import(self, operation: str, error: str) -> None:
        with self._lock:
            try:
                io_path(plain_path(self.root/f'{UUID(operation)}.part')).unlink(missing_ok=True)
            finally:
                if self._uploading == operation:
                    self._uploading = None
                self._update(operation, status='failed', stage='failed', error=error)

    def cancel(self, operation: str) -> dict[str, Any]:
        with self._lock:
            item = self.job(operation)
            if self._active == operation and item.get('status') in {'uploading','queued','running'}:
                self._cancel.set()
            return item

    def _import(self, operation: str, job: dict[str, Any]) -> dict[str, Any]:
        part = plain_path(self.root/f'{UUID(operation)}.part')
        try:
            result = self._verify(part, job['archive_sha256'], operation=operation)
            for candidate in self.operations.glob('*.json'):
                previous = self._read(candidate.stem)
                if previous and previous.get('id') != operation and previous.get('kind') in {'create','import'} and previous.get('status') == 'completed' and not previous.get('duplicate_of') and previous.get('archive_sha256') == job['archive_sha256']:
                    try:
                        if file_hash(self.download(previous['id']), self._cancelled) == job['archive_sha256']:
                            return {**result, 'duplicate_of': previous['id'], 'verification_status':'passed'}
                        self._update(previous['id'], verification_status='failed', verification_error='backup_hash_mismatch')
                    except (OSError, BackupError):
                        self._cancelled()
            self._cancelled()
            os.replace(io_path(part), io_path(self._archive(operation)))
            return {**result, 'verification_status':'passed'}
        finally:
            io_path(part).unlink(missing_ok=True)

    def _run(self, operation: str) -> None:
        job = self.job(operation)
        self._update(operation, status='running', stage='snapshot' if job['kind'] == 'create' else 'verify')
        archive_verified = False
        try:
            if job['kind'] == 'create':
                result = self._create(operation, job['include_pdfs'])
            elif job['kind'] == 'import':
                result = self._import(operation, job)
            else:
                target = self.job(job['backup_id'])
                result = self._verify(self.download(job['backup_id']), target['archive_sha256'],
                                      restore=job['kind'] in {'restore_preview', 'recover_workspace'}, operation=operation)
                archive_verified = True
                self._update(job['backup_id'], verified_at=now(), verification_status='passed')
                if job['kind'] == 'recover_workspace':
                    from literature_workbench.features.recovery import prepare_workspace
                    self._update(operation, stage='reconnecting')
                    result.update(prepare_workspace(Path(result['restore_preview']), self.inspector, self._cancelled))
                self._update(job['backup_id'], verified_at=now(), verification_status='passed',
                             **({'restore_preview': result['restore_preview']} if 'restore_preview' in result else {}))
            self._update(operation, status='completed', stage='completed', **result)
        except Exception as error:
            if not isinstance(error, BackupError):
                LOGGER.exception('Local backup operation failed')
            code = str(error) if isinstance(error, BackupError) else 'backup_io_error'
            self._update(operation, status='failed', stage='failed', error=code)
            if job['kind'] not in {'create','import'} and not archive_verified:
                self._update(job['backup_id'], verification_status='failed', verification_error=code)

    def recovery_workspace(self, operation: str) -> Path:
        from literature_workbench.features.recovery import validate_workspace
        job = self.job(operation)
        if job.get('kind') != 'recover_workspace' or job.get('status') != 'completed' or not job.get('recovery_ready'):
            raise BackupError('recovery_not_ready')
        expected = plain_path(self.root/'restore-previews'/str(UUID(operation))/'data')
        if job.get('workspace_root') != str(expected):
            raise BackupError('recovery_invalid_workspace')
        return validate_workspace(expected)

    def _create(self, operation: str, include_pdfs: bool) -> dict[str, Any]:
        part = plain_path(self.root / f'{operation}.part')
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=self.root) as temporary:
            snapshot = Path(temporary) / 'database.sqlite3'
            # SQLite's online backup includes committed WAL content and is a consistent DB snapshot.
            with self.database.connect() as live_connection, closing(sqlite3.connect(snapshot)) as target_connection:
                live_connection.backup(target_connection, pages=256, progress=lambda *_: self._cancelled())
            with closing(sqlite3.connect(snapshot)) as c:
                c.row_factory = sqlite3.Row
                self._check_database(c)
                docs = c.execute("SELECT d.id,d.path,d.content_sha256,d.display_name FROM documents d JOIN libraries l ON l.id=d.library_id WHERE l.status='active' AND d.format='pdf' AND d.status IN ('valid_pdf','candidate_pdf')").fetchall()
                counts = {table: c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in DATA_TABLES}
                versions = [r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')]
                recovery = recovery_files(c)
            manifest: dict[str, Any] = {'format': 'literature-workbench-backup', 'version': 1, 'created_at': now(),
                'data_root': str(self.data_root), 'include_pdfs': include_pdfs, 'counts': counts, 'schema_versions': versions,
                'files': [], 'documents': [], 'excluded': ['Windows Credential Manager API keys', 'model executables and weights', 'runtime sessions and staging', 'previous backups', 'inactive or unavailable linked PDF files'],
                'restore_note': '恢复副本保留原文件路径引用，不会自动替换当前数据库或原始PDF。切换或跨机器使用前需核对路径和文件身份。'}
            total_size = 0
            def add(stream: BinaryIO, name: str, archive: zipfile.ZipFile) -> dict[str, Any]:
                nonlocal total_size
                if len(manifest['files']) >= MAX_ENTRIES:
                    raise BackupError('backup_too_large')
                h = hashlib.sha256()
                size = 0
                with archive.open(name, 'w', force_zip64=True) as dest:
                    while chunk := stream.read(CHUNK):
                        self._cancelled()
                        total_size += len(chunk)
                        if total_size > MAX_BYTES:
                            raise BackupError('backup_too_large')
                        h.update(chunk)
                        dest.write(chunk)
                        size += len(chunk)
                record = {'path': name, 'sha256': h.hexdigest(), 'size': size}
                manifest['files'].append(record)
                if len(manifest['files']) % 10 == 0:
                    self._update(operation, stage='copying', processed_files=len(manifest['files']))
                return record
            try:
                with zipfile.ZipFile(part, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                    with snapshot.open('rb') as stream:
                        add(stream, 'data/db/literature-workbench.sqlite3', archive)
                    copied: dict[str, dict[str, Any]] = {}
                    if include_pdfs:
                        for folder in ('imports', 'parse-results', '.annotation-backups'):
                            base = plain_path(self.data_root / folder)
                            if not base.exists():
                                continue
                            for source in workspace_files(base):
                                # In-progress imports/journals aren't part of a committed workspace.
                                if source.suffix == '.tmp' or source.name.startswith('journal-'):
                                    continue
                                before = io_path(source).stat()
                                with io_path(source).open('rb') as stream:
                                    record = add(stream, 'data/' + source.relative_to(self.data_root).as_posix(), archive)
                                after = io_path(source).stat()
                                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                    raise BackupError('backup_source_changed')
                                copied[os.path.normcase(str(source.absolute()))] = record
                        for original, expected in recovery:
                            recovered = copied.get(os.path.normcase(str(ordinary_path(Path(original)))))
                            if recovered is None or recovered['sha256'] != expected:
                                raise BackupError('backup_recovery_missing')
                        for row in docs:
                            self._cancelled()
                            path = Path(row['path'])
                            document_record = copied.get(os.path.normcase(str(path.absolute())))
                            if document_record is None:
                                try:
                                    registered = load_registered_pdf(Database(snapshot), UUID(row['id']))
                                    handle = open_verified_pdf(self.inspector, registered)
                                except (DocumentIdentityChanged, DocumentNotFound, DocumentNotReadable):
                                    raise BackupError('backup_source_changed') from None
                                try:
                                    document_record = add(handle.stream, f'linked-pdfs/{row["id"]}.pdf', archive)
                                    if handle.current_identity() != handle.identity:
                                        raise BackupError('backup_source_changed')
                                finally:
                                    handle.close()
                            if document_record['sha256'] != row['content_sha256']:
                                raise BackupError('backup_source_changed')
                            manifest['documents'].append({'document_id': row['id'], 'display_name': row['display_name'],
                                'original_path': row['path'], 'archive_path': document_record['path'], 'sha256': document_record['sha256']})
                    archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                self._update(operation, stage='verify', processed_files=len(manifest['files']))
                archive_hash = file_hash(part, self._cancelled)
                self._verify(part, archive_hash, operation=operation)
                os.replace(part, self._archive(operation))
                return {'archive_sha256': archive_hash, 'size_bytes': self._archive(operation).stat().st_size,
                    'file_count': len(manifest['files']), 'pdf_count': len(manifest['documents']), 'counts': counts,
                    'verified_at': now(), 'verification_status': 'passed', 'processed_files': len(manifest['files'])}
            finally:
                if part.exists():
                    part.unlink()

    @staticmethod
    def _check_database(connection: sqlite3.Connection) -> None:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise BackupError('backup_database_invalid')

    def _verify(self, path: Path, expected_hash: str, *, operation: str, restore: bool = False) -> dict[str, Any]:
        from literature_workbench.features.backup_validation import bounded_archive, check_schema
        if file_hash(plain_path(path), self._cancelled) != expected_hash:
            raise BackupError('backup_hash_mismatch')
        restore_root = plain_path(self.root / 'restore-previews')
        restore_root.mkdir(exist_ok=True)
        destination = plain_path(restore_root / operation) if restore else None
        if destination:
            destination.mkdir()  # A unique candidate; never overwrite an existing directory.
        success = False
        try:
            with tempfile.TemporaryDirectory(prefix='.verify-', dir=self.root) as temporary, bounded_archive(path) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ENTRIES + 1 or len({i.filename.casefold() for i in infos}) != len(infos):
                    raise BackupError('backup_manifest_invalid')
                if any(entry.flag_bits & 1 for entry in infos):
                    raise BackupError('backup_manifest_invalid')
                info = archive.getinfo('manifest.json')
                if info.file_size > 32 * 1024**2:
                    raise BackupError('backup_manifest_invalid')
                manifest = json.loads(archive.read(info))
                if not isinstance(manifest, dict):
                    raise BackupError('backup_manifest_invalid')
                files = manifest.get('files')
                if manifest.get('format') != 'literature-workbench-backup' or type(manifest.get('version')) is not int or manifest.get('version') != 1 or not isinstance(files, list):
                    raise BackupError('backup_manifest_invalid')
                if not isinstance(manifest.get('include_pdfs'), bool) or not isinstance(manifest.get('created_at'), str):
                    raise BackupError('backup_manifest_invalid')
                if datetime.fromisoformat(manifest['created_at']).tzinfo is None:
                    raise BackupError('backup_manifest_invalid')
                if len(files) > MAX_ENTRIES or {f['path'] for f in files} | {'manifest.json'} != {i.filename for i in infos} or len({f['path'] for f in files}) != len(files):
                    raise BackupError('backup_manifest_invalid')
                total = 0
                database_path = Path(temporary) / 'verified.sqlite3'
                for record in files:
                    self._cancelled()
                    name = record['path']
                    if name != 'data/db/literature-workbench.sqlite3' and not name.startswith(('data/imports/', 'data/parse-results/', 'data/.annotation-backups/', 'linked-pdfs/')):
                        raise BackupError('backup_manifest_invalid')
                    if name.startswith('data/.annotation-backups/journal-'):
                        raise BackupError('backup_manifest_invalid')
                    parts = PurePosixPath(name).parts
                    if any(len(part.encode('utf-16-le')) > 510 for part in parts):
                        raise BackupError('backup_manifest_invalid')
                    if not parts or name != '/'.join(parts) or len(parts) < 2 or parts[0] not in {'data', 'linked-pdfs'} or any(p in {'..', '.', ''} or p.endswith(('.', ' ')) or p.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))} for p in parts) or '\\' in name or ':' in name or name.startswith('/') or any(ord(c) < 32 for c in name):
                        raise BackupError('backup_manifest_invalid')
                    entry = archive.getinfo(name)
                    if stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size != record['size'] or not isinstance(record['size'], int) or record['size'] < 0:
                        raise BackupError('backup_manifest_invalid')
                    total += entry.file_size
                    if total > MAX_BYTES:
                        raise BackupError('backup_too_large')
                    target = destination.joinpath(*parts) if destination else database_path if name == 'data/db/literature-workbench.sqlite3' else None
                    if target:
                        io_path(plain_path(target)).parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source:
                        with io_path(target).open('xb') if target else nullcontext() as output:
                            h = hashlib.sha256()
                            size = 0
                            while chunk := source.read(CHUNK):
                                self._cancelled()
                                size += len(chunk)
                                if size > entry.file_size:
                                    raise BackupError('backup_manifest_invalid')
                                h.update(chunk)
                                if output:
                                    output.write(chunk)
                            if size != record['size'] or h.hexdigest() != record['sha256']:
                                raise BackupError('backup_hash_mismatch')
                verified_db = destination / 'data/db/literature-workbench.sqlite3' if destination else database_path
                if not verified_db.is_file():
                    raise BackupError('backup_manifest_invalid')
                with closing(sqlite3.connect(f'{verified_db.as_uri()}?mode=ro', uri=True)) as c:
                    c.set_progress_handler(lambda: int(self._stop.is_set() or self._cancel.is_set()), 10_000)
                    check_schema(c, Path(temporary), manifest.get('schema_versions'))
                    self._check_database(c)
                    if {table: c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in DATA_TABLES} != manifest.get('counts'):
                        raise BackupError('backup_manifest_invalid')
                    docs = {r[0]: (r[1], r[2]) for r in c.execute("SELECT d.id,d.content_sha256,d.path FROM documents d JOIN libraries l ON l.id=d.library_id WHERE l.status='active' AND d.format='pdf' AND d.status IN ('valid_pdf','candidate_pdf')")}
                    recovery = recovery_files(c)
                records = {f['path']: f for f in files}
                saved_docs = manifest.get('documents', [])
                if not manifest['include_pdfs'] and (saved_docs != [] or list(records) != ['data/db/literature-workbench.sqlite3']):
                    raise BackupError('backup_manifest_invalid')
                if manifest.get('include_pdfs'):
                    for original, expected in recovery:
                        relative = ordinary_path(Path(original)).relative_to(ordinary_path(Path(manifest['data_root']))).as_posix()
                        if records.get('data/' + relative, {}).get('sha256') != expected:
                            raise BackupError('backup_recovery_missing')
                    if len(saved_docs) != len(docs) or {d['document_id'] for d in saved_docs} != set(docs):
                        raise BackupError('backup_manifest_invalid')
                    for doc in saved_docs:
                        if (doc['sha256'], doc['original_path']) != docs[doc['document_id']] or records[doc['archive_path']]['sha256'] != doc['sha256']:
                            raise BackupError('backup_manifest_invalid')
                if destination:
                    (destination/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
                    (destination/'恢复说明.txt').write_text(manifest['restore_note'], encoding='utf-8')
                success = True
                self._cancelled()
                return {'verified_files': len(files), 'file_count': len(files), 'verified_at': now(),
                        'include_pdfs': bool(manifest.get('include_pdfs')), 'pdf_count':len(saved_docs),
                        'counts': manifest['counts'], 'source_created_at':manifest['created_at'],
                        **({'restore_preview': str(destination)} if destination else {})}
        except (zipfile.BadZipFile, KeyError, TypeError, ValueError, sqlite3.Error, NotImplementedError):
            self._cancelled()
            raise BackupError('backup_manifest_invalid') from None
        finally:
            if destination and not success:
                # Only the fresh, fixed-root candidate created above is eligible.
                if destination.parent == restore_root and destination.name == operation:
                    shutil.rmtree(io_path(plain_path(destination)))
