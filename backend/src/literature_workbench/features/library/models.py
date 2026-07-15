from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NewType
from uuid import UUID


ContentId = NewType("ContentId", str)
DocumentId = NewType("DocumentId", UUID)


@dataclass(frozen=True, slots=True)
class ScanSummary:
    pdf_paths: int
    unique_contents: int
    duplicate_groups: int
    unsupported_paths: int
    queued_content_ids: tuple[ContentId, ...]
    unsafe_paths: int = 0


@dataclass(frozen=True, slots=True)
class LibraryRoot:
    id: UUID
    canonical_path: Path
    volume_id: str
    file_id: str
    recursive: bool
    discovery_mode: str
    authorization_source: str
    status: str


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: UUID
    library_root: LibraryRoot
    path: Path
    volume_id: str
    file_id: str
    size_bytes: int
    modified_ns: int
