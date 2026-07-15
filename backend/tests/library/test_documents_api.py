from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.filesystem import SafeWindowsFileInspector
from literature_workbench.security.local_session import LocalSession
from literature_workbench.worker import Worker, library_job_handlers


@dataclass(slots=True)
class FixedDialog:
    path: Path

    def select(self) -> Path:
        return self.path


def _headers(session: LocalSession) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": session.csrf_token}


def _pdf(marker: bytes) -> bytes:
    return b"%PDF-1.7\n" + marker + b"\n%%EOF"


def test_registration_initial_scan_worker_and_inventory_api_do_not_expose_absolute_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-library"
    root.mkdir()
    (root / "alpha.pdf").write_bytes(_pdf(b"alpha"))
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        folder_dialog=FixedDialog(root),
        file_inspector=SafeWindowsFileInspector(),
        register_initial_library=False,
    )
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        selection = client.post("/api/libraries/select-root", headers=_headers(session))
        registration = client.post(
            "/api/libraries",
            headers=_headers(session),
            json={"selection_token": selection.json()["selection_token"], "recursive": True},
        )
        library_id = registration.json()["library_id"]
        queue = TaskQueue(client.app.state.database, client.app.state.queue_clock)
        assert queue.count(kind="scan_library") == 1
        with client.app.state.database.connect() as connection:
            revision = connection.execute(
                "SELECT revision FROM app_settings WHERE key = 'active_library_revision'"
            ).fetchone()["revision"]
        assert revision == 1

        worker = Worker(
            queue,
            "test-worker",
            library_job_handlers(client.app.state.library_scanner),
        )
        assert client.portal.call(worker.run_once) is True
        assert queue.count(kind="parse_pdf") == 1

        listing = client.get("/api/documents?q=ALPHA&page=1&page_size=20")
        item = listing.json()["items"][0]
        detail = client.get(f"/api/documents/{item['document_id']}")
        scan_status = client.get(f"/api/libraries/{library_id}/scan-status")

    assert registration.status_code == 201
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert detail.status_code == 200
    assert scan_status.status_code == 200
    assert scan_status.json()["last_scan"]["pdf_paths"] == 1
    assert scan_status.json()["last_scan"]["warnings"] == []
    assert set(item) == {
        "document_id",
        "current_document_version_id",
        "content_id",
        "etag",
        "filename",
        "title",
        "authors",
        "relative_folder",
        "size_bytes",
        "status",
        "duplicate_count",
        "parsing_state",
        "cloud_policy",
    }
    assert item["filename"] == "alpha.pdf"
    assert item["relative_folder"] == ""
    assert item["title"] is None
    assert item["authors"] is None
    assert item["cloud_policy"]["auto_cloud_eligible"] is False
    assert str(root) not in listing.text
    assert str(root) not in detail.text
    assert detail.headers["etag"] == f'"{item["content_id"]}"'


def test_duplicate_registration_does_not_duplicate_initial_job_or_revision(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        folder_dialog=FixedDialog(root),
        file_inspector=SafeWindowsFileInspector(),
        register_initial_library=False,
    )
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        ids = []
        for _ in range(2):
            selection = client.post("/api/libraries/select-root", headers=_headers(session))
            registration = client.post(
                "/api/libraries",
                headers=_headers(session),
                json={"selection_token": selection.json()["selection_token"]},
            )
            ids.append(registration.json()["library_id"])
        with client.app.state.database.connect() as connection:
            job_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind = 'scan_library'"
            ).fetchone()[0]
            revision = connection.execute(
                "SELECT revision FROM app_settings WHERE key = 'active_library_revision'"
            ).fetchone()[0]

    assert ids[0] == ids[1]
    assert job_count == 1
    assert revision == 1


def test_registration_rolls_back_root_when_initial_job_insert_fails(tmp_path: Path) -> None:
    class FailingQueue(TaskQueue):
        def enqueue_in_transaction(self, *args, **kwargs):
            raise sqlite3.IntegrityError("injected_job_failure")

    root = tmp_path / "library"
    root.mkdir()
    database = Database.open(tmp_path / "db.sqlite3")
    run_migrations(database)
    repository = LibraryRepository(
        database, FailingQueue(database, lambda: datetime.now(UTC))
    )
    identity = SafeWindowsFileInspector().inspect_root_directory(root)

    with pytest.raises(sqlite3.IntegrityError, match="injected_job_failure"):
        repository.register(
            identity,
            recursive=True,
            authorization_source="native_picker_confirmed",
        )

    assert repository.count() == 0


def test_manual_scan_requires_session_and_csrf_and_uses_deduplicated_job(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        folder_dialog=FixedDialog(root),
        file_inspector=SafeWindowsFileInspector(),
        register_initial_library=False,
    )
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        selection = client.post("/api/libraries/select-root", headers=_headers(session))
        registered = client.post(
            "/api/libraries",
            headers=_headers(session),
            json={"selection_token": selection.json()["selection_token"]},
        )
        library_id = registered.json()["library_id"]
        missing_csrf = client.post(
            f"/api/libraries/{library_id}/scan", headers={"Origin": "http://testserver"}
        )
        accepted = client.post(f"/api/libraries/{library_id}/scan", headers=_headers(session))
        with client.app.state.database.connect() as connection:
            jobs = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind = 'scan_library'"
            ).fetchone()[0]
        client.cookies.clear()
        missing_session = client.post(
            f"/api/libraries/{library_id}/scan", headers=_headers(session)
        )

    assert missing_csrf.json() == {"code": "csrf_required"}
    assert accepted.status_code == 202
    assert jobs == 1
    assert missing_session.json() == {"code": "session_required"}


def test_composed_app_includes_every_task_five_route(tmp_path: Path) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path),
        local_session=session,
        register_initial_library=False,
    )
    missing_id = "00000000-0000-0000-0000-000000000001"
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        assert client.get("/api/documents").status_code == 200
        assert client.get(f"/api/documents/{missing_id}").json() == {
            "code": "document_not_found"
        }
        assert client.get(f"/api/libraries/{missing_id}/scan-status").json() == {
            "code": "library_not_found"
        }
        assert client.post(
            f"/api/libraries/{missing_id}/scan",
            headers={"Origin": "http://127.0.0.1", "X-LW-CSRF": session.csrf_token},
        ).json() == {"code": "library_not_found"}
