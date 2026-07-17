from __future__ import annotations

import hashlib
import os
from pathlib import Path

import fitz
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.security.local_session import LocalSession


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _headers(session: LocalSession) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": session.csrf_token}


def _source_pdf(path: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=400, height=500)
        page.insert_text((72, 90), "A precise sentence for annotation testing.")
        historical = page.add_highlight_annot(fitz.Rect(70, 72, 260, 96))
        historical.set_info(title="Other PDF editor", content="historical note")
        historical.update()
        document.save(path)


def _registered_client(tmp_path: Path) -> tuple[TestClient, LocalSession, Path, str]:
    library = tmp_path / "library"
    library.mkdir()
    source = library / "paper.pdf"
    _source_pdf(source)
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        file_inspector=SafeWindowsFileInspector(),
        register_initial_library=False,
    )
    client = TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    )
    client.__enter__()
    identity = client.app.state.file_inspector.inspect_root_directory(library)
    registered = client.app.state.library_repository.register(
        identity, recursive=True, authorization_source="test"
    )
    client.app.state.library_scanner.scan_library(registered.id)
    item = client.get("/api/documents").json()["items"][0]
    return client, session, source, item["document_id"]


def _annotation_request(detail: dict[str, object]) -> dict[str, object]:
    return {
        "document_version_id": detail["current_document_version_id"],
        "content_id": detail["content_id"],
        "page": 1,
        "type": "highlight",
        "quads": [[72, 410, 260, 410, 72, 428, 260, 428]],
        "color": "yellow",
        "comment": "",
    }


def test_annotation_rejects_same_content_path_replacement(tmp_path: Path) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        detail = client.get(f"/api/documents/{document_id}").json()
        original = source.stat()
        replacement = source.with_name("replacement.pdf")
        replacement.write_bytes(source.read_bytes())
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, source)

        response = client.post(
            f"/api/documents/{document_id}/annotations",
            headers=_headers(session),
            json=_annotation_request(detail),
        )

        assert response.status_code == 409
        assert response.json() == {"code": "annotation_external_change"}
    finally:
        client.__exit__(None, None, None)


def test_annotation_rejects_new_hard_link_alias(tmp_path: Path) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        detail = client.get(f"/api/documents/{document_id}").json()
        os.link(source, source.with_name("alias.pdf"))

        response = client.post(
            f"/api/documents/{document_id}/annotations",
            headers=_headers(session),
            json=_annotation_request(detail),
        )

        assert response.status_code == 409
        assert response.json() == {"code": "annotation_external_change"}
    finally:
        client.__exit__(None, None, None)


def test_comment_writeback_edit_undo_and_baseline_restore(tmp_path: Path) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        original_hash = _hash(source)
        detail = client.get(f"/api/documents/{document_id}").json()
        existing = client.get(f"/api/documents/{document_id}/annotations")
        assert existing.status_code == 200
        assert existing.json()["items"][0]["owned"] is False
        historical_id = existing.json()["items"][0]["annotation_id"]

        create = client.post(
            f"/api/documents/{document_id}/annotations",
            headers=_headers(session),
            json={
                "document_version_id": detail["current_document_version_id"],
                "content_id": detail["content_id"],
                "page": 1,
                "type": "comment",
                "quads": [[72, 410, 260, 410, 72, 428, 260, 428]],
                "color": "yellow",
                "comment": "first comment",
            },
        )
        assert create.status_code == 201, create.text
        assert _hash(source) != original_hash
        annotation_id = create.json()["annotation_id"]
        baseline = (
            tmp_path / "data" / ".annotation-backups" / document_id / "baseline.pdf"
        )
        assert baseline.is_file()
        assert _hash(baseline) == original_hash

        listed = client.get(f"/api/documents/{document_id}/annotations").json()
        created = next(item for item in listed["items"] if item["annotation_id"] == annotation_id)
        assert created["type"] == "comment"
        assert created["owned"] is True
        assert created["comment"] == "first comment"

        read_only_delete = client.request(
            "DELETE",
            f"/api/documents/{document_id}/annotations/{historical_id}",
            headers=_headers(session),
            json={
                "document_version_id": listed["current_document_version_id"],
                "content_id": listed["content_id"],
            },
        )
        assert read_only_delete.status_code == 409
        assert read_only_delete.json() == {"code": "annotation_read_only"}

        update = client.patch(
            f"/api/documents/{document_id}/annotations/{annotation_id}",
            headers=_headers(session),
            json={
                "document_version_id": listed["current_document_version_id"],
                "content_id": listed["content_id"],
                "color": "pink",
                "comment": "edited comment",
            },
        )
        assert update.status_code == 200, update.text
        undo = client.post(
            f"/api/documents/{document_id}/annotations/undo", headers=_headers(session)
        )
        assert undo.status_code == 200, undo.text
        after_undo = client.get(f"/api/documents/{document_id}/annotations").json()
        restored = next(item for item in after_undo["items"] if item["annotation_id"] == annotation_id)
        assert restored["comment"] == "first comment"

        restore = client.post(
            f"/api/documents/{document_id}/annotations/restore-baseline",
            headers=_headers(session),
            json={"confirm_filename": "paper.pdf"},
        )
        assert restore.status_code == 200, restore.text
        assert _hash(source) == original_hash
        final = client.get(f"/api/documents/{document_id}/annotations").json()["items"]
        assert all(item["owned"] is False for item in final)
        assert any(item["comment"] == "historical note" for item in final)
    finally:
        client.__exit__(None, None, None)


def test_annotation_rejects_stale_document_identity_without_touching_pdf(tmp_path: Path) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        before = _hash(source)
        detail = client.get(f"/api/documents/{document_id}").json()
        response = client.post(
            f"/api/documents/{document_id}/annotations",
            headers=_headers(session),
            json={
                "document_version_id": detail["current_document_version_id"],
                "content_id": "0" * 64,
                "page": 1,
                "type": "highlight",
                "quads": [[72, 410, 260, 410, 72, 428, 260, 428]],
                "color": "yellow",
                "comment": "",
            },
        )
        assert response.status_code == 409
        assert response.json() == {"code": "annotation_document_version_conflict"}
        assert _hash(source) == before
    finally:
        client.__exit__(None, None, None)
