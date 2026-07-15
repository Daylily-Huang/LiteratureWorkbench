from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.security.local_session import LocalSession


@dataclass(frozen=True, slots=True)
class RegisteredPdfFixture:
    client: TestClient
    session: LocalSession
    document_id: UUID
    document_version_id: UUID
    content_id: str
    pdf_path: Path
    pdf_bytes: bytes

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Cookie": f"lw_session={self.session.session_cookie}"}


@pytest.fixture
def registered_pdf(tmp_path: Path) -> Iterator[RegisteredPdfFixture]:
    library_path = tmp_path / "library"
    library_path.mkdir()
    pdf_bytes = b"%PDF-" + bytes((index % 251 for index in range(1019)))
    assert len(pdf_bytes) == 1024
    pdf_path = library_path / "fixture.pdf"
    pdf_path.write_bytes(pdf_bytes)
    inspector = SafeWindowsFileInspector()
    root_identity = inspector.inspect_root_directory(library_path)
    library_id = uuid4()
    library = LibraryRoot(
        id=library_id,
        canonical_path=root_identity.canonical_path,
        volume_id=root_identity.volume_id,
        file_id=root_identity.file_id,
        recursive=True,
        discovery_mode="monitored",
        authorization_source="test_registered",
        status="active",
    )
    identity = inspector.inspect_under_root(pdf_path, library)
    content_id = hashlib.sha256(pdf_bytes).hexdigest()
    document_id = uuid4()
    version_id = uuid4()
    timestamp = datetime.now(UTC).isoformat()
    session = LocalSession()
    app = create_app(
        AppSettings(
            data_root=tmp_path / "isolated-data",
            allowed_hosts=("testserver",),
        ),
        local_session=session,
        file_inspector=inspector,
        register_initial_library=False,
    )
    with TestClient(app, base_url="http://testserver") as client:
        with client.app.state.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO libraries(
                  id,root_path,root_path_key,root_volume_id,root_file_id,recursive,
                  discovery_mode,cloud_pdf_auto_allowed,authorization_source,status,
                  created_at,updated_at)
                VALUES(?,?,?,?,?,1,'monitored',0,'test_registered','active',?,?)
                """,
                (
                    str(library_id),
                    str(root_identity.canonical_path),
                    os.path.normcase(os.path.abspath(root_identity.canonical_path)),
                    root_identity.volume_id,
                    root_identity.file_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO contents(sha256,size_bytes,mime_type,status,created_at,updated_at)
                VALUES(?,?,'application/pdf','queued',?,?)
                """,
                (content_id, len(pdf_bytes), timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO documents(
                  id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                  mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                  auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                VALUES(?,?,?,?,?,'fixture.pdf','pdf',?,?,?,?,?,'parse',0,
                       'test_registered','valid_pdf',?,?)
                """,
                (
                    str(document_id),
                    str(library_id),
                    content_id,
                    str(identity.canonical_path),
                    os.path.normcase(os.path.abspath(identity.canonical_path)),
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
        yield RegisteredPdfFixture(
            client=client,
            session=session,
            document_id=document_id,
            document_version_id=version_id,
            content_id=content_id,
            pdf_path=identity.canonical_path,
            pdf_bytes=pdf_bytes,
        )
