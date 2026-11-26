from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

import fitz  # type: ignore[import-untyped]

from literature_workbench.db.database import Database
from literature_workbench.features.library.fingerprint import fingerprint_verified_pdf
from literature_workbench.features.library.models import ContentId, DocumentRecord, LibraryRoot
from literature_workbench.features.parser.models import StagedPdf
from literature_workbench.features.parser.workspace import (
    TrustedParserFile,
    TrustedParserWorkspace,
)
from literature_workbench.features.reader.pdf_stream import (
    DocumentIdentityChanged,
    RegisteredPdf,
    open_verified_pdf,
)
from literature_workbench.platform.windows.filesystem import (
    FileIdentity,
    SafeFileInspector,
    VerifiedHandle,
)


STAGING_COPY_CHUNK_BYTES = 4 * 1024 * 1024
CONTENT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_METADATA_TITLE_CODEPOINTS = 500
MAX_METADATA_AUTHOR_CODEPOINTS = 200
MAX_METADATA_AUTHORS = 20


class AllSourcesUnavailable(RuntimeError):
    pass


class StagedContentMismatch(ValueError):
    pass


class StagingCheckpointSink(Protocol):
    def record_copy_started(self, attempt_id: UUID) -> None: ...

    def record_staged(self, relative_path: str, sha256: str) -> None: ...


@dataclass(frozen=True, slots=True)
class StructureValidation:
    status: Literal["valid_pdf", "invalid_pdf"]
    staged: StagedPdf
    title: str | None
    authors: tuple[str, ...]
    error_code: str | None


def open_current_source_for_content(
    database: Database,
    inspector: SafeFileInspector,
    content_id: ContentId,
    *,
    reconcile: Callable[[UUID, Path], object] | None = None,
) -> VerifiedHandle:
    expected_hash = _validated_content_id(content_id)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT d.*,l.root_path,l.root_volume_id,l.root_file_id,l.recursive,
                   l.discovery_mode,l.authorization_source AS root_authorization_source,
                   l.status AS root_status
            FROM documents AS d
            JOIN libraries AS l ON l.id=d.library_id
            WHERE d.content_sha256=? AND d.format='pdf'
              AND d.status IN ('candidate_pdf','valid_pdf') AND l.status='active'
            ORDER BY d.path_key
            """,
            (expected_hash,),
        ).fetchall()
    for row in rows:
        registered = _registered_pdf_from_row(row)
        handle: VerifiedHandle | None = None
        try:
            handle = open_verified_pdf(inspector, registered)
            fingerprint = fingerprint_verified_pdf(handle)
            current = handle.current_identity()
            if (
                fingerprint.sha256 != expected_hash
                or fingerprint.size_bytes != registered.size
                or current != handle.identity
            ):
                raise DocumentIdentityChanged("source_changed_during_second_hash")
            handle.stream.seek(0)
            return handle
        except (OSError, ValueError):
            if handle is not None:
                handle.close()
            if reconcile is not None:
                reconcile(registered.document.library_root.id, registered.document.path)
    raise AllSourcesUnavailable("all_registered_sources_unavailable")


def stage_verified_pdf(
    data_root: Path | TrustedParserWorkspace,
    content_id: ContentId,
    source: VerifiedHandle,
    attempt_id: UUID,
    checkpoint: StagingCheckpointSink,
    *,
    chunk_size: int = STAGING_COPY_CHUNK_BYTES,
    cancellation: object | None = None,
) -> StagedPdf:
    expected_hash = _validated_content_id(content_id)
    if chunk_size <= 0 or chunk_size > STAGING_COPY_CHUNK_BYTES:
        raise ValueError("invalid_staging_chunk_size")
    workspace, owns_workspace = _workspace(data_root)
    part: TrustedParserFile | None = None
    journal: TrustedParserFile | None = None
    succeeded = False
    guarded = False
    checkpoint.record_copy_started(attempt_id)
    digest = hashlib.sha256()
    source.stream.seek(0)
    try:
        part = workspace.create_staging_file(content_id, attempt_id)
        if owns_workspace:
            part.adopt_owner(workspace)
        while chunk := source.stream.read(chunk_size):
            if cancellation is not None:
                cancellation.raise_if_cancelled()  # type: ignore[attr-defined]
            part.write(chunk)
            digest.update(chunk)
        part.flush()
        actual_hash = digest.hexdigest()
        current = source.current_identity()
        if actual_hash != expected_hash or current != source.identity:
            raise StagedContentMismatch("staged_content_hash_or_identity_mismatch")
        if part.sha256(cancellation) != expected_hash:
            raise StagedContentMismatch("staged_handle_hash_mismatch")
        journal = workspace.create_journal(content_id, attempt_id)
        journal.append_journal_record(
            workspace.journal_payload(content_id, attempt_id, actual_hash, "prepared")
        )
        final_path = part.path.with_suffix(".pdf")
        part.rename(final_path)
        journal.append_journal_record(
            workspace.journal_payload(content_id, attempt_id, actual_hash, "renamed")
        )
        part = workspace.bind_staged_read_guard(part, actual_hash)
        guarded = True
        relative_path = _relative_staging_name(content_id, attempt_id, ".pdf")
        checkpoint.record_staged(relative_path, actual_hash)
        staged = StagedPdf(
            content_id, attempt_id, final_path, actual_hash, None, held_file=part
        )
        succeeded = True
        return staged
    finally:
        if part is not None and not succeeded and guarded:
            if journal is not None:
                journal.close()
                journal = None
            workspace.cleanup_staged(
                part,
                content_id,
                attempt_id,
                expected_hash,
            )
            part = None
        elif part is not None and not succeeded:
            try:
                part.delete()
                if journal is not None:
                    journal.delete()
            except (OSError, ValueError):
                pass
        if journal is not None:
            journal.close()
        if part is not None and not succeeded:
            part.close()
        elif part is None and owns_workspace:
            workspace.close()


def recover_checkpointed_staging(
    data_root: Path | TrustedParserWorkspace,
    content_id: ContentId,
    attempt_id: UUID,
    relative_path: str,
    expected_hash: ContentId,
) -> StagedPdf | None:
    _validated_content_id(content_id)
    expected = str(_validated_content_id(expected_hash))
    suffix = Path(relative_path).suffix
    if suffix not in {".part", ".pdf"}:
        return None
    exact_relative = _relative_staging_name(content_id, attempt_id, suffix)
    if Path(relative_path).as_posix() != exact_relative:
        return None
    workspace, owns_workspace = _workspace(data_root)
    held: TrustedParserFile | None = None
    journal: TrustedParserFile | None = None
    recovered = False
    try:
        recovery_suffix = ".pdf" if suffix == ".part" else suffix
        held = workspace.open_staging_file(content_id, attempt_id, recovery_suffix)
        if owns_workspace:
            held.adopt_owner(workspace)
        actual_hash = held.sha256()
        if actual_hash != expected:
            return None
        journal = workspace.open_staging_file(
            content_id, attempt_id, ".promotion.json"
        )
        record = journal.read_latest_journal_record(64 * 1024)
        expected_record = json.loads(
            workspace.journal_payload(
                content_id, attempt_id, actual_hash, "renamed"
            ).decode("utf-8")
        )
        if record != expected_record:
            return None
        staged = StagedPdf(
            content_id, attempt_id, held.path, actual_hash, None, held_file=held
        )
        recovered = True
        return staged
    except (FileNotFoundError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        if journal is not None:
            journal.close()
        if held is not None and not recovered:
            held.close()
        elif held is None and owns_workspace:
            workspace.close()


def cleanup_checkpointed_staging(
    data_root: Path,
    content_id: ContentId,
    attempt_id: UUID,
    relative_path: str,
) -> bool:
    """Refuse legacy path-only cleanup.

    Deletion is only safe while the exact staged file capability is still held and
    its promotion journal, identity, UUID, and hash can all be revalidated.  The
    legacy checkpoint contains no trusted hash and therefore cannot authorize a
    destructive recovery action.  Call ``TrustedParserWorkspace.cleanup_staged``
    with the held capability instead.
    """
    del data_root
    _validated_content_id(content_id)
    suffix = Path(relative_path).suffix
    if suffix not in {".part", ".pdf"}:
        return False
    exact_relative = _relative_staging_name(content_id, attempt_id, suffix)
    if Path(relative_path).as_posix() != exact_relative:
        return False
    return False


def validate_staged_structure(staged: StagedPdf) -> StructureValidation:
    try:
        with fitz.open(staged.path) as document:
            if document.needs_pass or document.is_repaired or document.page_count <= 0:
                raise ValueError("invalid_pdf_structure")
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                if page.rect.width <= 0 or page.rect.height <= 0:
                    raise ValueError("invalid_pdf_page_dimensions")
            metadata = document.metadata or {}
            title = _bounded_metadata(str(metadata.get("title") or ""))
            authors = _split_authors(str(metadata.get("author") or ""))
            validated = replace(staged, page_count=document.page_count)
        return StructureValidation("valid_pdf", validated, title, authors, None)
    except Exception:
        return StructureValidation(
            "invalid_pdf",
            replace(staged, page_count=None),
            None,
            (),
            "invalid_pdf_structure",
        )


def persist_structure_validation(
    database: Database,
    validation: StructureValidation,
    source_identity: FileIdentity | VerifiedHandle,
    *,
    cancellation: object | None = None,
    inspector: SafeFileInspector | None = None,
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    valid = validation.status == "valid_pdf"
    primary_identity = (
        source_identity.identity
        if isinstance(source_identity, VerifiedHandle)
        else source_identity
    )
    if isinstance(source_identity, VerifiedHandle):
        _verify_commit_inputs(validation, source_identity, cancellation)
    with database.transaction() as connection:
        _fence(cancellation, connection)
        if not isinstance(source_identity, VerifiedHandle):
            _verify_commit_inputs(validation, source_identity, cancellation)
        connection.execute(
            """
            UPDATE contents
            SET page_count=?,title=?,authors_json=?,status=?,updated_at=?
            WHERE sha256=?
            """,
            (
                validation.staged.page_count if valid else None,
                validation.title if valid else None,
                (
                    json.dumps(validation.authors, ensure_ascii=False, separators=(",", ":"))
                    if valid and validation.authors
                    else None
                ),
                validation.status,
                timestamp,
                validation.staged.content_id,
            ),
        )
        rows = connection.execute(
            """
            SELECT d.*,l.root_path,l.root_volume_id,l.root_file_id,l.recursive,
                   l.discovery_mode,l.authorization_source AS root_authorization_source,
                   l.status AS root_status,l.cloud_pdf_auto_allowed,
                   EXISTS (
                     SELECT 1 FROM confirmed_imports AS ci
                     JOIN import_intents AS ii ON ii.id=ci.intent_id
                     WHERE ci.document_id=d.id AND ci.content_sha256=d.content_sha256
                       AND ci.final_path_key=d.path_key AND ii.status='completed'
                       AND ii.content_sha256=ci.content_sha256
                       AND ii.final_path_key=ci.final_path_key
                   ) AS has_confirmed_import
            FROM documents AS d JOIN libraries AS l ON l.id=d.library_id
            WHERE d.content_sha256=?
            """,
            (validation.staged.content_id,),
        ).fetchall()
        for row in rows:
            eligible = valid and _document_cloud_eligible(
                row,
                validation.staged.content_id,
                inspector,
                cancellation,
                primary_identity,
            )
            connection.execute(
                "UPDATE documents SET auto_cloud_eligible=?,updated_at=? WHERE id=?",
                (int(eligible), timestamp, str(row["id"])),
            )
        _fence(cancellation, connection)


def _verify_commit_inputs(
    validation: StructureValidation,
    source: FileIdentity | VerifiedHandle,
    cancellation: object | None,
) -> None:
    if cancellation is None:
        return
    hash_cancellation = (
        cancellation if hasattr(cancellation, "raise_if_cancelled") else None
    )
    if not validation.staged.verify_current_hash(hash_cancellation):
        raise StagedContentMismatch("staged_identity_changed_before_commit")
    if not isinstance(source, VerifiedHandle):
        raise StagedContentMismatch("held_source_required_for_fenced_commit")
    fingerprint = fingerprint_verified_pdf(source)
    current = source.current_identity()
    if (
        fingerprint.sha256 != validation.staged.content_id
        or current != source.identity
    ):
        raise StagedContentMismatch("source_changed_before_commit")
    source.stream.seek(0)


def _document_cloud_eligible(
    row: sqlite3.Row,
    content_id: ContentId,
    inspector: SafeFileInspector | None,
    cancellation: object | None,
    primary_identity: FileIdentity,
) -> bool:
    authorization = str(row["authorization_source"])
    authorized = authorization == "registered_library" or (
        authorization == "confirmed_import"
        and str(row["discovery_mode"]) == "managed_import"
        and str(row["root_authorization_source"]) == "managed_import_internal"
        and bool(row["has_confirmed_import"])
    )
    if (
        not authorized
        or not bool(row["cloud_pdf_auto_allowed"])
        or str(row["root_status"]) != "active"
        or not str(row["root_authorization_source"] or "")
        or str(row["status"]) not in {"candidate_pdf", "valid_pdf"}
        or int(row["hard_link_count"]) != 1
    ):
        return False
    if inspector is None:
        return (
            bool(row["cloud_pdf_auto_allowed"])
            and str(row["library_id"]) == str(primary_identity.root_id)
            and os.path.normcase(os.path.abspath(str(row["path"])))
            == os.path.normcase(os.path.abspath(primary_identity.canonical_path))
            and str(row["volume_id"]) == primary_identity.volume_id
            and str(row["file_id"]) == primary_identity.file_id
            and int(row["size_bytes"]) == primary_identity.size
            and int(row["mtime_ns"]) == primary_identity.modified_ns
            and int(row["hard_link_count"]) == primary_identity.hard_link_count == 1
        )
    handle: VerifiedHandle | None = None
    try:
        if cancellation is not None:
            cancellation.raise_if_cancelled()  # type: ignore[attr-defined]
        registered = _registered_pdf_from_row(row)
        handle = open_verified_pdf(inspector, registered)
        fingerprint = fingerprint_verified_pdf(handle)
        current = handle.current_identity()
        return (
            fingerprint.sha256 == content_id
            and fingerprint.size_bytes == int(row["size_bytes"])
            and current == handle.identity
            and current.hard_link_count == 1
        )
    except (OSError, ValueError):
        return False
    finally:
        if handle is not None:
            handle.close()


def _fence(cancellation: object | None, connection: sqlite3.Connection) -> None:
    if cancellation is not None:
        cancellation.require_current_lease(connection)  # type: ignore[attr-defined]


def _registered_pdf_from_row(row: sqlite3.Row) -> RegisteredPdf:
    root = LibraryRoot(
        id=UUID(str(row["library_id"])),
        canonical_path=Path(str(row["root_path"])),
        volume_id=str(row["root_volume_id"]),
        file_id=str(row["root_file_id"]),
        recursive=bool(row["recursive"]),
        discovery_mode=str(row["discovery_mode"]),
        authorization_source=str(row["root_authorization_source"]),
        status=str(row["root_status"]),
    )
    document = DocumentRecord(
        id=UUID(str(row["id"])),
        library_root=root,
        path=Path(str(row["path"])),
        volume_id=str(row["volume_id"]),
        file_id=str(row["file_id"]),
        size_bytes=int(row["size_bytes"]),
        modified_ns=int(row["mtime_ns"]),
    )
    return RegisteredPdf(
        document=document,
        content_id=str(row["content_sha256"]),
        hard_link_count=int(row["hard_link_count"]),
    )


def _workspace(
    data_root: Path | TrustedParserWorkspace,
) -> tuple[TrustedParserWorkspace, bool]:
    if isinstance(data_root, TrustedParserWorkspace):
        return data_root, False
    return TrustedParserWorkspace.create(data_root), True


def _validated_content_id(content_id: ContentId) -> str:
    value = str(content_id)
    if CONTENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_content_id")
    return value


def _relative_staging_name(
    content_id: ContentId, attempt_id: UUID, suffix: str
) -> str:
    if suffix not in {".part", ".pdf"}:
        raise ValueError("invalid_staging_suffix")
    return f"staging/{_validated_content_id(content_id)}/{attempt_id}{suffix}"


def _bounded_metadata(raw: str) -> str | None:
    clean = "".join(
        " " if character.isspace() else character
        for character in raw
        if not unicodedata.category(character).startswith("C") or character.isspace()
    )
    normalized = " ".join(clean.split())
    if not normalized:
        return None
    return "".join(list(normalized)[:MAX_METADATA_TITLE_CODEPOINTS])


def _split_authors(raw: str) -> tuple[str, ...]:
    authors: list[str] = []
    for item in re.split(r"[;|\r\n]+", raw):
        normalized = _bounded_metadata(item)
        if normalized is None:
            continue
        authors.append("".join(list(normalized)[:MAX_METADATA_AUTHOR_CODEPOINTS]))
        if len(authors) == MAX_METADATA_AUTHORS:
            break
    return tuple(authors)
