from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import fitz
import pytest
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


def test_annotation_crash_journal_recovery(tmp_path: Path) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        data_root = tmp_path / "data"
        backup_root = data_root / ".annotation-backups"
        backup_root.mkdir(parents=True, exist_ok=True)

        original_hash = _hash(source)
        # Create a backup snapshot of original file
        fake_snapshot = backup_root / "fake_snapshot.pdf"
        fake_snapshot.write_bytes(source.read_bytes())

        # Simulate corrupted / half-written file on disk
        source.write_bytes(b"%PDF-corrupted-content")
        assert _hash(source) != original_hash

        # Write an orphaned crash journal
        journal_file = backup_root / f"journal-{document_id}.json"
        journal_file.write_text(
            json.dumps({
                "document_id": document_id,
                "path": str(source),
                "pre_hash": original_hash,
                "backup_path": str(fake_snapshot),
                "created_at": "2026-08-20T00:00:00Z",
            }),
            encoding="utf-8",
        )

        # Trigger recovery
        client.app.state.annotation_service.reconcile_crashed_mutations()

        # Target file restored to pre-mutation hash and journal cleaned up
        assert _hash(source) == original_hash
        assert not journal_file.exists()
    finally:
        client.__exit__(None, None, None)


def test_atomic_mutation_rollback_on_db_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        original_hash = _hash(source)
        detail = client.get(f"/api/documents/{document_id}").json()

        service = client.app.state.annotation_service
        def failing_record(*args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("simulated_db_crash")
        monkeypatch.setattr(service, "_record_version_and_mutation", failing_record)

        with pytest.raises(RuntimeError, match="simulated_db_crash"):
            service.create(
                UUID(str(detail["document_id"])),
                document_version_id=UUID(str(detail["current_document_version_id"])),
                content_id=str(detail["content_id"]),
                page_number=1,
                annotation_type="highlight",
                quads=[[72, 410, 260, 410, 72, 428, 260, 428]],
                color="yellow",
                comment="",
            )

        assert _hash(source) == original_hash
    finally:
        client.__exit__(None, None, None)


def test_undo_rollback_on_db_error_preserves_current_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session, source, document_id = _registered_client(tmp_path)
    try:
        detail = client.get(f"/api/documents/{document_id}").json()
        service = client.app.state.annotation_service

        # Perform mutation A -> B
        service.create(
            UUID(str(detail["document_id"])),
            document_version_id=UUID(str(detail["current_document_version_id"])),
            content_id=str(detail["content_id"]),
            page_number=1,
            annotation_type="highlight",
            quads=[[72, 410, 260, 410, 72, 428, 260, 428]],
            color="yellow",
            comment="",
        )
        state_b_hash = _hash(source)

        # Simulate failure during undo DB record
        def failing_undo_record(*args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("simulated_undo_db_crash")

        monkeypatch.setattr(service, "_record_undo_version", failing_undo_record)

        with pytest.raises(RuntimeError, match="simulated_undo_db_crash"):
            service.undo(UUID(str(detail["document_id"])))

        # Crucial invariant: if undo fails mid-way, source PDF must remain at State B
        assert _hash(source) == state_b_hash
    finally:
        client.__exit__(None, None, None)


def test_postcommit_pruning_failure_preserves_pdf_and_db_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session, source, doc_id = _registered_client(tmp_path)
    try:
        detail = client.get(f"/api/documents/{doc_id}").json()
        service = client.app.state.annotation_service
        original_hash = _hash(source)

        def failing_prune(*args: object, **kwargs: object) -> None:
            raise PermissionError("simulated locked old snapshot")

        monkeypatch.setattr(service, "_prune_snapshots", failing_prune)

        # Mutation must succeed even if pruning old snapshots encounters a lock/permission error
        result = service.create(
            UUID(doc_id),
            document_version_id=UUID(detail["current_document_version_id"]),
            content_id=detail["content_id"],
            page_number=1,
            annotation_type="highlight",
            quads=[[72, 410, 260, 410, 72, 428, 260, 428]],
            color="yellow",
            comment="",
        )
        assert result["content_id"] != original_hash
        assert _hash(source) == result["content_id"]

        after = client.get(f"/api/documents/{doc_id}").json()
        assert after["content_id"] == result["content_id"]

        # Annotations read must return 200 without version conflict
        annotations_resp = client.get(f"/api/documents/{doc_id}/annotations")
        assert annotations_resp.status_code == 200
        assert len(annotations_resp.json()["items"]) == 2

        # Crash recovery journal must be clean
        backup_root = tmp_path / "data" / ".annotation-backups"
        assert not (backup_root / f"journal-{doc_id}.json").exists()
    finally:
        client.__exit__(None, None, None)


def test_crash_recovery_preserves_externally_replaced_file(tmp_path: Path) -> None:
    client, session, source, doc_id = _registered_client(tmp_path)
    try:
        service = client.app.state.annotation_service
        backup_root = tmp_path / "data" / ".annotation-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / "snapshot.pdf"
        backup.write_bytes(source.read_bytes())
        before_hash = _hash(source)

        # External editor replaces the file with a new file
        replacement = source.with_name("replacement.pdf")
        _source_pdf(replacement)
        with fitz.open(replacement) as pdf:
            pdf[0].insert_text((72, 150), "External edit that must survive")
            pdf.saveIncr()
        replacement.replace(source)
        external_hash = _hash(source)
        assert external_hash != before_hash

        journal = backup_root / f"journal-{doc_id}.json"
        journal.write_text(
            json.dumps({
                "document_id": doc_id,
                "path": str(source),
                "pre_hash": before_hash,
                "backup_path": str(backup),
            }),
            encoding="utf-8",
        )

        service.reconcile_crashed_mutations()

        # External replacement must NOT be overwritten!
        assert _hash(source) == external_hash
        assert backup.exists()
        assert journal.exists()
    finally:
        client.__exit__(None, None, None)


def test_crash_recovery_corrupted_backup_preserves_target_and_journal(tmp_path: Path) -> None:
    client, session, source, doc_id = _registered_client(tmp_path)
    try:
        service = client.app.state.annotation_service
        backup_root = tmp_path / "data" / ".annotation-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / "snapshot.pdf"
        backup.write_bytes(b"%PDF-corrupted-snapshot-bytes")
        before_hash = _hash(source)

        # Corrupt file on disk
        source.write_bytes(b"%PDF-half-written-on-disk")

        journal = backup_root / f"journal-{doc_id}.json"
        journal.write_text(
            json.dumps({
                "document_id": doc_id,
                "path": str(source),
                "pre_hash": before_hash,
                "backup_path": str(backup),
            }),
            encoding="utf-8",
        )

        service.reconcile_crashed_mutations()

        # Because backup hash != pre_hash, recovery must fail-closed and preserve journal
        assert journal.exists()
        assert source.read_bytes() == b"%PDF-half-written-on-disk"
    finally:
        client.__exit__(None, None, None)


def test_crash_recovery_inactive_library_preserves_target_and_journal(tmp_path: Path) -> None:
    client, session, source, doc_id = _registered_client(tmp_path)
    try:
        service = client.app.state.annotation_service
        backup_root = tmp_path / "data" / ".annotation-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / "snapshot.pdf"
        backup.write_bytes(source.read_bytes())
        before_hash = _hash(source)

        source.write_bytes(b"%PDF-half-written")

        # Deactivate library in DB
        with client.app.state.database.transaction() as conn:
            conn.execute("UPDATE libraries SET status = 'disabled'")

        journal = backup_root / f"journal-{doc_id}.json"
        journal.write_text(
            json.dumps({
                "document_id": doc_id,
                "path": str(source),
                "pre_hash": before_hash,
                "backup_path": str(backup),
            }),
            encoding="utf-8",
        )

        service.reconcile_crashed_mutations()

        # Inactive library: cannot recover or overwrite
        assert journal.exists()
        assert source.read_bytes() == b"%PDF-half-written"
    finally:
        client.__exit__(None, None, None)
