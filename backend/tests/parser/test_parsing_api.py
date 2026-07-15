from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.models import ParsedPage
from literature_workbench.features.parser.normalizer import ParseRunRepository
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.security.local_session import LocalSession


def _register_document(client: TestClient, tmp_path: Path) -> tuple[UUID, ContentId]:
    library_path = tmp_path / "api-library"
    library_path.mkdir()
    pdf_path = library_path / "api.pdf"
    payload = b"%PDF-api-fixture"
    pdf_path.write_bytes(payload)
    inspector = SafeWindowsFileInspector()
    root = inspector.inspect_root_directory(library_path)
    library_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    identity_root = __import__(
        "literature_workbench.features.library.models", fromlist=["LibraryRoot"]
    ).LibraryRoot(
        id=library_id,
        canonical_path=root.canonical_path,
        volume_id=root.volume_id,
        file_id=root.file_id,
        recursive=True,
        discovery_mode="monitored",
        authorization_source="api_test",
        status="active",
    )
    identity = inspector.inspect_under_root(pdf_path, identity_root)
    content_id = ContentId(hashlib.sha256(payload).hexdigest())
    timestamp = datetime.now(UTC).isoformat()
    with client.app.state.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO libraries(
              id,root_path,root_path_key,root_volume_id,root_file_id,recursive,
              discovery_mode,cloud_pdf_auto_allowed,authorization_source,status,
              created_at,updated_at)
            VALUES(?,?,?,?,?,1,'monitored',0,'api_test','active',?,?)
            """,
            (
                str(library_id), str(root.canonical_path),
                os.path.normcase(os.path.abspath(root.canonical_path)),
                root.volume_id, root.file_id, timestamp, timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO contents(
              sha256,size_bytes,mime_type,page_count,status,created_at,updated_at)
            VALUES(?,?,'application/pdf',2,'valid_pdf',?,?)
            """,
            (content_id, len(payload), timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO documents(
              id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
              mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
              auto_cloud_eligible,authorization_source,status,created_at,updated_at)
            VALUES(?,?,?,?,?,'api.pdf','pdf',?,?,?,?,1,'parse',0,
                   'registered_library','valid_pdf',?,?)
            """,
            (
                str(document_id), str(library_id), content_id, str(identity.canonical_path),
                os.path.normcase(os.path.abspath(identity.canonical_path)), identity.size,
                identity.modified_ns, identity.volume_id, identity.file_id, timestamp, timestamp,
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
                str(version_id), str(document_id), content_id, identity.size,
                identity.modified_ns, identity.volume_id, identity.file_id, 1, timestamp,
            ),
        )
    return document_id, content_id


def _page(content_id: ContentId, run_id: UUID, index: int, text: str) -> ParsedPage:
    return ParsedPage(
        content_id=content_id,
        page_index=index,
        physical_page=index + 1,
        parse_run_id=run_id,
        parser_name="pymupdf",
        parser_version="test-version",
        normalized_text=text,
        raw_text=f"raw {text}",
        width_points=400,
        height_points=600,
        coordinate_reliable=True,
        blocks=[],
    )


def test_composed_parse_api_enqueues_bounded_requests_and_updates_preference(
    tmp_path: Path,
) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    headers = {"Origin": "http://testserver", "X-LW-CSRF": session.csrf_token}
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        document_id, content_id = _register_document(client, tmp_path)
        default = client.post(f"/api/documents/{document_id}/parse", headers=headers, json={})
        forced = client.post(
            f"/api/documents/{document_id}/parse",
            headers=headers,
            json={"strategy": "mineru", "ocr_language": "ch"},
        )
        read_only = client.patch(
            f"/api/documents/{document_id}/parse-preference",
            headers=headers,
            json={"preference": "read_only"},
        )
        parse_again = client.patch(
            f"/api/documents/{document_id}/parse-preference",
            headers=headers,
            json={"preference": "parse"},
        )
        status = client.get(f"/api/documents/{document_id}/parse-status")
        with client.app.state.database.connect() as connection:
            jobs = connection.execute(
                "SELECT payload_json FROM jobs WHERE kind='parse_pdf' ORDER BY rowid"
            ).fetchall()
            document = connection.execute(
                "SELECT parse_preference,parse_strategy_override,ocr_language_override "
                "FROM documents WHERE id=?",
                (str(document_id),),
            ).fetchone()
            content = connection.execute(
                "SELECT parse_request_revision FROM contents WHERE sha256=?",
                (str(content_id),),
            ).fetchone()

    assert default.status_code == 202
    assert forced.status_code == 202
    assert read_only.status_code == 200
    assert parse_again.status_code == 202
    assert len(jobs) == 3
    assert json.loads(jobs[0]["payload_json"]) == {
        "document_id": str(document_id),
        "content_id": str(content_id),
        "strategy": "auto",
        "ocr_language": None,
        "request_revision": 1,
    }
    assert json.loads(jobs[1]["payload_json"])["strategy"] == "mineru"
    assert json.loads(jobs[1]["payload_json"])["ocr_language"] == "ch"
    assert [json.loads(row["payload_json"])["request_revision"] for row in jobs] == [
        1,
        2,
        3,
    ]
    assert content is not None and int(content["parse_request_revision"]) == 3
    assert document is not None
    assert tuple(document) == ("parse", "mineru", "ch")
    assert status.status_code == 200
    assert status.json()["preference"] == "parse"
    assert "output_dir" not in status.text


def test_text_api_reads_only_current_completed_run_and_maps_physical_page(
    tmp_path: Path,
) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        document_id, content_id = _register_document(client, tmp_path)
        repository = ParseRunRepository(client.app.state.database)
        with client.app.state.database.transaction() as connection:
            allocated = connection.execute(
                """
                UPDATE contents SET parse_request_revision=parse_request_revision+1
                WHERE sha256=? RETURNING parse_request_revision
                """,
                (str(content_id),),
            ).fetchone()
        assert allocated is not None
        run_id = repository.create_run(
            content_id,
            "pymupdf",
            "test-version",
            request_revision=int(allocated["parse_request_revision"]),
        )
        repository.complete_run(
            run_id,
            [_page(content_id, run_id, 0, "page one"), _page(content_id, run_id, 1, "page two")],
        )

        response = client.get(f"/api/documents/{document_id}/pages/2/text")
        missing_page = client.get(f"/api/documents/{document_id}/pages/3/text")

    assert response.status_code == 200
    assert response.json()["physical_page"] == 2
    assert response.json()["page_index"] == 1
    assert response.json()["normalized_text"] == "page two"
    assert response.json()["raw_text"] == "raw page two"
    assert response.json()["blocks"] == []
    assert "output_dir" not in response.text
    assert "staging" not in response.text
    assert missing_page.status_code == 404


def test_text_api_uses_repository_document_page_snapshot_only(tmp_path: Path) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    document_id = uuid4()
    content_id = ContentId("a" * 64)
    run_id = uuid4()
    expected = _page(content_id, run_id, 0, "snapshot page")

    class SnapshotRepository:
        def read_document_current_page(
            self, requested_document_id: UUID, physical_page: int
        ) -> tuple[bool, ParsedPage | None]:
            assert requested_document_id == document_id
            assert physical_page == 1
            return True, expected

        def read_current_page(self, *_args: object) -> ParsedPage | None:
            raise AssertionError("split content/page snapshot must not be used")

    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        original_database = client.app.state.database
        client.app.state.database = object()
        client.app.state.parse_run_repository = SnapshotRepository()
        try:
            response = client.get(f"/api/documents/{document_id}/pages/1/text")
        finally:
            client.app.state.database = original_database

    assert response.status_code == 200
    assert response.json()["normalized_text"] == "snapshot page"


def test_parse_api_rejects_unbounded_values_and_requires_composed_route(
    tmp_path: Path,
) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    headers = {"Origin": "http://testserver", "X-LW-CSRF": session.csrf_token}
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        document_id, _ = _register_document(client, tmp_path)
        invalid_strategy = client.post(
            f"/api/documents/{document_id}/parse",
            headers=headers,
            json={"strategy": "shell", "ocr_language": None},
        )
        invalid_language = client.post(
            f"/api/documents/{document_id}/parse",
            headers=headers,
            json={"strategy": "auto", "ocr_language": "fr"},
        )
        missing = client.get(
            "/api/documents/00000000-0000-0000-0000-000000000001/parse-status"
        )

    assert invalid_strategy.status_code == 422
    assert invalid_language.status_code == 422
    assert missing.status_code == 404


def test_parse_api_rejects_missing_current_pdf_before_enqueue(tmp_path: Path) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    headers = {"Origin": "http://testserver", "X-LW-CSRF": session.csrf_token}
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        document_id, _ = _register_document(client, tmp_path)
        with client.app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT path FROM documents WHERE id=?", (str(document_id),)
            ).fetchone()
        assert row is not None
        Path(str(row["path"])).unlink()

        response = client.post(
            f"/api/documents/{document_id}/parse", headers=headers, json={}
        )
        with client.app.state.database.connect() as connection:
            jobs = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE kind='parse_pdf'"
            ).fetchone()

    assert response.status_code == 409
    assert jobs is not None and int(jobs["count"]) == 0
