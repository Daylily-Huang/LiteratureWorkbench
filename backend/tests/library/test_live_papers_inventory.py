from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector


PAPERS_ROOT = Path(r"D:\black-muntjac-project\papers")


def _stable_manifest(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    rows: list[tuple[str, int, int, str]] = []
    paths = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    for path in paths:
        metadata = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                digest.update(chunk)
        rows.append(
            (
                path.relative_to(root).as_posix(),
                metadata.st_size,
                metadata.st_mtime_ns,
                digest.hexdigest(),
            )
        )
    return tuple(rows)


@pytest.mark.live_library
def test_real_papers_inventory_is_scanned_read_only(tmp_path: Path) -> None:
    if os.environ.get("LW_RUN_LIVE_LIBRARY_TESTS") != "1":
        pytest.skip("set LW_RUN_LIVE_LIBRARY_TESTS=1 to scan the approved live library")
    before = _stable_manifest(PAPERS_ROOT)
    database = Database.open(tmp_path / "isolated.sqlite3")
    run_migrations(database)
    inspector = SafeWindowsFileInspector()
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    repository = LibraryRepository(database, queue)
    library = repository.register(
        inspector.inspect_root_directory(PAPERS_ROOT),
        recursive=True,
        authorization_source="initial_user_approved",
    )
    scanner = LibraryScanner(database, repository, inspector, queue)

    summary = scanner.scan_library(library.id)
    after = _stable_manifest(PAPERS_ROOT)

    assert before == after
    assert summary.pdf_paths == 145
    assert summary.unique_contents == 127
    assert summary.pdf_paths - summary.unique_contents == 18
    assert summary.duplicate_groups == 18
    # The current approved root has README.md plus one DOCX; both are metadata-only.
    assert summary.unsupported_paths == 2
