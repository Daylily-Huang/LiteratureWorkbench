from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.security.local_session import LocalSession
from literature_workbench.worker import Worker, library_job_handlers


def test_folder_list_requires_session_and_reports_actual_scan(client, authenticated, tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.7\nfolder-management\n%%EOF")
    state = client.app.state
    library = state.library_repository.register(
        state.file_inspector.inspect_root_directory(root), recursive=True,
        authorization_source="test_selected",
    )
    before = client.get("/api/libraries").json()["items"]
    assert before[0]["scan_state"] == "queued"
    worker = Worker(state.task_queue, "folder-test", library_job_handlers(state.library_scanner))
    assert client.portal.call(worker.run_once)
    item = client.get("/api/libraries").json()["items"][0]
    assert item["library_id"] == str(library.id)
    assert item["display_path"] == str(root)
    assert item["recursive"] is True
    assert item["document_count"] == 1
    assert item["scan_state"] == "completed"
    assert item["last_synced_at"]
    assert item["warnings"] == []
    with pytest.raises(ValueError, match="library_recursive_mismatch"):
        state.library_repository.register(
            state.file_inspector.inspect_root_directory(root), recursive=False,
            authorization_source="test_selected",
        )
    assert state.library_repository.get(library.id).recursive is True
    client.cookies.clear()
    assert client.get("/api/libraries").status_code == 403


def test_restart_enqueues_scan_and_discovers_offline_additions(tmp_path: Path):
    root = tmp_path / "papers"
    root.mkdir()
    (root / "before.pdf").write_bytes(b"%PDF-1.7\nbefore\n%%EOF")
    settings = AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",))
    session = LocalSession()
    def app():
        return create_app(settings, local_session=session, register_initial_library=False)

    with TestClient(app(), base_url="http://testserver", cookies={"lw_session": session.session_cookie}) as first:
        state = first.app.state
        state.library_repository.register(
            state.file_inspector.inspect_root_directory(root), recursive=True,
            authorization_source="test_selected",
        )
        worker = Worker(state.task_queue, "first", library_job_handlers(state.library_scanner))
        assert first.portal.call(worker.run_once)
        assert first.get("/api/documents").json()["total"] == 1
    (root / "while-closed.pdf").write_bytes(b"%PDF-1.7\nwhile-closed\n%%EOF")
    with TestClient(app(), base_url="http://testserver", cookies={"lw_session": session.session_cookie}) as second:
        state = second.app.state
        with state.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='scan_library' AND status='queued'").fetchone()[0] == 1
        worker = Worker(state.task_queue, "second", library_job_handlers(state.library_scanner))
        assert second.portal.call(worker.run_once)
        items = second.get("/api/documents").json()["items"]
        assert {item["filename"] for item in items} == {"before.pdf", "while-closed.pdf"}
        assert len({item["library_id"] for item in items}) == 1
        assert {item["library_name"] for item in items} == {f"{root.parent.name} / papers"}
