from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


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
