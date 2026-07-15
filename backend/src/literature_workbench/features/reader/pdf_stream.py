from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from literature_workbench.db.database import Database
from literature_workbench.features.library.models import DocumentRecord, LibraryRoot
from literature_workbench.platform.windows.filesystem import (
    SafeFileInspector,
    UnsafePath,
    VerifiedHandle,
)


STREAM_CHUNK_BYTES = 1024 * 1024


class DocumentNotFound(LookupError):
    pass


class DocumentNotReadable(ValueError):
    pass


class DocumentIdentityChanged(ValueError):
    pass


class RangeNotSatisfiable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class RegisteredPdf:
    document: DocumentRecord
    content_id: str
    hard_link_count: int

    @property
    def size(self) -> int:
        return self.document.size_bytes

    @property
    def etag(self) -> str:
        return f'"sha256:{self.content_id}"'


def parse_single_range(value: str | None, size: int) -> ByteRange | None:
    if value is None:
        return None
    if size < 0 or not value.startswith("bytes=") or "," in value:
        raise RangeNotSatisfiable("invalid_byte_range")
    spec = value[6:]
    if spec.count("-") != 1:
        raise RangeNotSatisfiable("invalid_byte_range")
    raw_start, raw_end = spec.split("-", 1)
    if raw_start == "":
        if not raw_end.isdecimal():
            raise RangeNotSatisfiable("invalid_byte_range")
        suffix_length = int(raw_end)
        if suffix_length <= 0 or size == 0:
            raise RangeNotSatisfiable("invalid_byte_range")
        return ByteRange(max(0, size - suffix_length), size - 1)
    if not raw_start.isdecimal() or (raw_end != "" and not raw_end.isdecimal()):
        raise RangeNotSatisfiable("invalid_byte_range")
    start = int(raw_start)
    if start >= size:
        raise RangeNotSatisfiable("invalid_byte_range")
    end = size - 1 if raw_end == "" else min(int(raw_end), size - 1)
    if end < start:
        raise RangeNotSatisfiable("invalid_byte_range")
    return ByteRange(start, end)


def load_registered_pdf(database: Database, document_id: UUID) -> RegisteredPdf:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT d.*,l.root_path,l.root_volume_id,l.root_file_id,l.recursive,
                   l.discovery_mode,l.authorization_source AS root_authorization_source,
                   l.status AS root_status
            FROM documents AS d
            JOIN libraries AS l ON l.id=d.library_id
            WHERE d.id=?
            """,
            (str(document_id),),
        ).fetchone()
    if row is None:
        raise DocumentNotFound("document_not_found")
    if (
        str(row["format"]).casefold() != "pdf"
        or str(row["status"]) not in {"candidate_pdf", "valid_pdf"}
        or row["content_sha256"] is None
    ):
        raise DocumentNotReadable("document_not_pdf")
    return _registered_pdf_from_row(row)


def open_verified_pdf(
    inspector: SafeFileInspector, registered: RegisteredPdf
) -> VerifiedHandle:
    handle: VerifiedHandle | None = None
    try:
        handle = inspector.open_registered_pdf(registered.document)
        current = handle.current_identity()
    except (OSError, UnsafePath) as error:
        if handle is not None:
            handle.close()
        raise DocumentIdentityChanged("document_identity_changed") from error
    expected = registered.document
    if (
        current.root_id != expected.library_root.id
        or current.volume_id != expected.volume_id
        or current.file_id != expected.file_id
        or current.size != expected.size_bytes
        or current.modified_ns != expected.modified_ns
        or current.hard_link_count != registered.hard_link_count
        or current.is_reparse_point
        or os.path.normcase(os.path.abspath(current.canonical_path))
        != os.path.normcase(os.path.abspath(expected.path))
    ):
        handle.close()
        raise DocumentIdentityChanged("document_identity_changed")
    return handle


def stream_pdf(
    handle: VerifiedHandle,
    byte_range: ByteRange,
    *,
    chunk_size: int = STREAM_CHUNK_BYTES,
) -> Iterator[bytes]:
    if chunk_size <= 0 or chunk_size > STREAM_CHUNK_BYTES:
        handle.close()
        raise ValueError("invalid_stream_chunk_size")
    try:
        handle.stream.seek(byte_range.start)
        remaining = byte_range.length
        while remaining > 0:
            chunk = handle.stream.read(min(chunk_size, remaining))
            if not chunk:
                raise DocumentIdentityChanged("document_truncated_during_stream")
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


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
