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


class UnsafeStagingPath(ValueError):
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
    data_root: Path,
    content_id: ContentId,
    source: VerifiedHandle,
    attempt_id: UUID,
    checkpoint: StagingCheckpointSink,
    *,
    chunk_size: int = STAGING_COPY_CHUNK_BYTES,
) -> StagedPdf:
    expected_hash = _validated_content_id(content_id)
    if chunk_size <= 0 or chunk_size > STAGING_COPY_CHUNK_BYTES:
        raise ValueError("invalid_staging_chunk_size")
    directory = _staging_directory(data_root, content_id)
    part_path = directory / f"{attempt_id}.part"
    final_path = directory / f"{attempt_id}.pdf"
    checkpoint.record_copy_started(attempt_id)
    digest = hashlib.sha256()
    source.stream.seek(0)
    with part_path.open("xb") as destination:
        while chunk := source.stream.read(chunk_size):
            destination.write(chunk)
            digest.update(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    actual_hash = digest.hexdigest()
    try:
        current = source.current_identity()
    except Exception as error:
        raise StagedContentMismatch("source_identity_unavailable_after_copy") from error
    if actual_hash != expected_hash or current != source.identity:
        raise StagedContentMismatch("staged_content_hash_or_identity_mismatch")
    if final_path.exists():
        raise FileExistsError(final_path)
    os.rename(part_path, final_path)
    relative_path = _relative_staging_name(content_id, attempt_id, ".pdf")
    checkpoint.record_staged(relative_path, actual_hash)
    return StagedPdf(content_id, attempt_id, final_path, actual_hash, None)


def recover_checkpointed_staging(
    data_root: Path,
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
    directory = _staging_directory(data_root, content_id)
    path = directory / f"{attempt_id}{suffix}"
    if not path.exists():
        return None
    if suffix == ".part":
        path.unlink()
        return None
    actual_hash = _hash_owned_file(path)
    if actual_hash != expected:
        path.unlink()
        return None
    return StagedPdf(content_id, attempt_id, path, actual_hash, None)


def cleanup_checkpointed_staging(
    data_root: Path,
    content_id: ContentId,
    attempt_id: UUID,
    relative_path: str,
) -> bool:
    _validated_content_id(content_id)
    suffix = Path(relative_path).suffix
    if suffix not in {".part", ".pdf"}:
        return False
    exact_relative = _relative_staging_name(content_id, attempt_id, suffix)
    if Path(relative_path).as_posix() != exact_relative:
        return False
    directory = _staging_directory(data_root, content_id)
    path = directory / f"{attempt_id}{suffix}"
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


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
    source_identity: FileIdentity,
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    valid = validation.status == "valid_pdf"
    with database.transaction() as connection:
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
            SELECT d.id,d.library_id,d.path,d.volume_id,d.file_id,d.size_bytes,d.mtime_ns,
                   d.hard_link_count,l.cloud_pdf_auto_allowed
            FROM documents AS d JOIN libraries AS l ON l.id=d.library_id
            WHERE d.content_sha256=?
            """,
            (validation.staged.content_id,),
        ).fetchall()
        for row in rows:
            same_source = (
                str(row["library_id"]) == str(source_identity.root_id)
                and os.path.normcase(os.path.abspath(str(row["path"])))
                == os.path.normcase(os.path.abspath(source_identity.canonical_path))
                and str(row["volume_id"]) == source_identity.volume_id
                and str(row["file_id"]) == source_identity.file_id
                and int(row["size_bytes"]) == source_identity.size
                and int(row["mtime_ns"]) == source_identity.modified_ns
                and int(row["hard_link_count"]) == source_identity.hard_link_count == 1
            )
            eligible = valid and bool(row["cloud_pdf_auto_allowed"]) and same_source
            connection.execute(
                "UPDATE documents SET auto_cloud_eligible=?,updated_at=? WHERE id=?",
                (int(eligible), timestamp, str(row["id"])),
            )


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


def _staging_directory(data_root: Path, content_id: ContentId) -> Path:
    root = Path(os.path.abspath(data_root))
    root.mkdir(parents=True, exist_ok=True)
    staging = root / "staging"
    directory = staging / _validated_content_id(content_id)
    for candidate in (root, staging, directory):
        candidate.mkdir(exist_ok=True)
        resolved = candidate.resolve(strict=True)
        if (
            os.path.normcase(os.path.abspath(resolved))
            != os.path.normcase(os.path.abspath(candidate))
            or candidate.is_symlink()
            or (hasattr(candidate, "is_junction") and candidate.is_junction())
        ):
            raise UnsafeStagingPath("staging_directory_is_not_application_owned")
    return directory


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


def _hash_owned_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(STAGING_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


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
