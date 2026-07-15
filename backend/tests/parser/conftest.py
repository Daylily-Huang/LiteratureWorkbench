from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.library.models import ContentId, LibraryRoot
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector


@dataclass(frozen=True, slots=True)
class RegisteredParserPdf:
    document_id: UUID
    content_id: ContentId
    path: Path
    payload: bytes


@dataclass(slots=True)
class ParserLibraryFixture:
    database: Database
    data_root: Path
    library_path: Path
    library: LibraryRoot
    inspector: SafeWindowsFileInspector

    def register(self, relative_name: str, payload: bytes) -> RegisteredParserPdf:
        path = self.library_path / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        identity = self.inspector.inspect_under_root(path, self.library)
        content_id = ContentId(hashlib.sha256(payload).hexdigest())
        document_id = uuid4()
        version_id = uuid4()
        timestamp = datetime.now(UTC).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO contents(
                  sha256,size_bytes,mime_type,status,created_at,updated_at)
                VALUES(?,?,'application/pdf','queued',?,?)
                """,
                (content_id, len(payload), timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO documents(
                  id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                  mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                  auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?, 'pdf',?,?,?,?,?,'parse',0,
                       'registered_library','valid_pdf',?,?)
                """,
                (
                    str(document_id),
                    str(self.library.id),
                    content_id,
                    str(identity.canonical_path),
                    os.path.normcase(os.path.abspath(identity.canonical_path)),
                    identity.canonical_path.name,
                    identity.size,
                    identity.modified_ns,
                    identity.volume_id,
                    identity.file_id,
                    identity.hard_link_count,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO document_versions(
                  id,document_id,content_sha256,size_bytes,mtime_ns,volume_id,file_id,
                  hard_link_count,observed_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(version_id),
                    str(document_id),
                    content_id,
                    identity.size,
                    identity.modified_ns,
                    identity.volume_id,
                    identity.file_id,
                    identity.hard_link_count,
                    timestamp,
                ),
            )
        return RegisteredParserPdf(document_id, content_id, identity.canonical_path, payload)


@pytest.fixture
def parser_library(tmp_path: Path) -> ParserLibraryFixture:
    data_root = tmp_path / "application-data"
    database = Database.open(data_root / "db" / "literature-workbench.sqlite3")
    run_migrations(database)
    library_path = tmp_path / "temporary-library"
    library_path.mkdir()
    inspector = SafeWindowsFileInspector()
    root_identity = inspector.inspect_root_directory(library_path)
    library = LibraryRoot(
        id=uuid4(),
        canonical_path=root_identity.canonical_path,
        volume_id=root_identity.volume_id,
        file_id=root_identity.file_id,
        recursive=True,
        discovery_mode="monitored",
        authorization_source="parser_test",
        status="active",
    )
    timestamp = datetime.now(UTC).isoformat()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO libraries(
              id,root_path,root_path_key,root_volume_id,root_file_id,recursive,
              discovery_mode,cloud_pdf_auto_allowed,authorization_source,status,
              created_at,updated_at)
            VALUES(?,?,?,?,?,1,'monitored',1,'parser_test','active',?,?)
            """,
            (
                str(library.id),
                str(library.canonical_path),
                os.path.normcase(os.path.abspath(library.canonical_path)),
                library.volume_id,
                library.file_id,
                timestamp,
                timestamp,
            ),
        )
    return ParserLibraryFixture(database, data_root, library_path, library, inspector)
