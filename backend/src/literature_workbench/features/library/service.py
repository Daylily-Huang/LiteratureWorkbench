from __future__ import annotations

from uuid import UUID

from literature_workbench.features.library.models import ScanSummary
from literature_workbench.features.library.scanner import LibraryScanner


class LibraryService:
    def __init__(self, scanner: LibraryScanner) -> None:
        self._scanner = scanner

    def scan_library(self, library_id: UUID) -> ScanSummary:
        return self._scanner.scan_library(library_id)
