import sqlite3
from uuid import uuid4

from literature_workbench.api.workspace import archive_answer
from literature_workbench.worker import Worker, library_job_handlers


def headers(authenticated):
    return {"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf}


def seed(client, tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    (root / "paper.pdf").write_bytes(b"%PDF-1.7\nworkspace\n%%EOF")
    state = client.app.state
    state.library_repository.register(state.file_inspector.inspect_root_directory(root), recursive=True, authorization_source="test")
    worker = Worker(state.task_queue, "workspace-test", library_job_handlers(state.library_scanner))
    assert client.portal.call(worker.run_once)
    return client.get("/api/documents").json()["items"][0]["document_id"]


def test_reading_patches_preserve_other_fields_and_persist(client, authenticated, tmp_path):
    doc = seed(client, tmp_path)
    url = f"/api/workspace/reading/{doc}"
    h = headers(authenticated)
    assert client.patch(url, headers=h, json={"favorite": True, "tags": [" 方法 ", "方法"], "readingStatus": "read"}).status_code == 200
    result = client.patch(url, headers=h, json={"page": 12, "totalPages": 20}).json()
    assert result["favorite"] is True and result["tags"] == ["方法"] and result["readingStatus"] == "read"
    assert client.get("/api/workspace/reading").json()["items"][doc]["page"] == 12
    # A separate connection sees committed state, independent of browser origin/port.
    with client.app.state.database.connect() as connection:
        assert connection.execute("SELECT page FROM reading_records WHERE document_id=?", (doc,)).fetchone()[0] == 12
    for payload in ({"page": 0}, {"favorite": None}, {"tags": ["a" * 61]}, {"readingStatus": "invented"}):
        assert client.patch(url, headers=h, json=payload).status_code == 422
    assert client.patch(url, json={"page": 3}).status_code == 403
    assert client.patch(f"/api/workspace/reading/{uuid4()}", headers=h, json={"page": 3}).status_code == 404


def test_notebook_preserves_original_answer_conflicts_and_detects_changed_sources(client, authenticated, tmp_path):
    doc = seed(client, tmp_path)
    db = client.app.state.database
    with db.connect() as c:
        content = c.execute("SELECT content_sha256 FROM documents WHERE id=?", (doc,)).fetchone()[0]
    body = {"answer": "Original answer", "scope": "selected", "citations": [{"document_id": doc, "content_id": content, "physical_page": 1, "excerpt": "Original quote", "display_name": "paper.pdf", "rects": []}]}
    entry_id = archive_answer(db, "Compare evidence", body, [doc])
    entry = client.get("/api/workspace/notebook").json()["items"][0]
    assert entry["answer"]["citations"][0]["source_changed"] is False
    edit = {"title": "My research", "note": "My independent interpretation", "starred": True, "revision": 0}
    url = f"/api/workspace/notebook/{entry_id}"
    assert client.patch(url, headers=headers(authenticated), json=edit).json() == {"revision": 1}
    assert client.patch(url, headers=headers(authenticated), json=edit).status_code == 409
    assert client.patch(url, headers=headers(authenticated), json={**edit, "answer": "tampered"}).status_code == 422
    with db.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (doc,))
    entry = client.get("/api/workspace/notebook").json()["items"][0]
    assert entry["answer"]["answer"] == "Original answer"
    assert entry["answer"]["citations"][0]["excerpt"] == "Original quote"
    assert entry["answer"]["citations"][0]["source_changed"] is True
    assert entry["note"] == edit["note"] and entry["document_ids"] == [doc]


def test_manual_notes_and_read_authorization(client, authenticated):
    response = client.post("/api/workspace/notebook", headers=headers(authenticated), json={"title": "研究计划", "note": "待验证"})
    assert response.status_code == 201
    entry = client.get("/api/workspace/notebook").json()["items"][0]
    assert entry["answer"] == {} and entry["question"] == "" and entry["starred"] is True
    client.cookies.clear()
    assert client.get("/api/workspace/notebook").status_code == 403
    assert client.get("/api/workspace/reading").status_code == 403


def test_scope_contract_and_archive_failure_are_explicit(client, authenticated, monkeypatch):
    from literature_workbench.features.research.service import LibraryAnswer

    class Service:
        def ask_library(self, *, question, document_ids):
            assert document_ids == [selected]
            return LibraryAnswer(answer="No evidence", citations=[], provider="retrieval_only", model="", indexed_documents=0, citation_status="missing")

    selected = str(uuid4())
    client.app.state.research_service = Service()
    for ids in ([], ["bad"], [str(uuid4()) for _ in range(9)]):
        assert client.post("/api/libraries/ask", headers=headers(authenticated), json={"question": "Compare", "document_ids": ids}).status_code == 422
    response = client.post("/api/libraries/ask", headers=headers(authenticated), json={"question": "Compare", "document_ids": [selected]})
    assert response.json()["history_saved"] is True
    entry = client.get("/api/workspace/notebook").json()["items"][0]
    assert entry["document_ids"] == [selected] and entry["answer"]["scope"] == "selected"
    def fail(*args):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr("literature_workbench.api.research.archive_answer", fail)
    response = client.post("/api/libraries/ask", headers=headers(authenticated), json={"question": "Compare", "document_ids": [selected]})
    assert response.status_code == 200 and response.json()["history_saved"] is False and "entry_id" not in response.json()
