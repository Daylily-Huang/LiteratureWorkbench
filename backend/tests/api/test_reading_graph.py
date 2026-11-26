from uuid import uuid4

import fitz

from literature_workbench.api.workspace import archive_answer
from literature_workbench.worker import Worker, library_job_handlers


def seed(client, tmp_path):
    root = tmp_path / "papers"
    folder = root / "methods"
    folder.mkdir(parents=True)
    for name in ("first", "second", "third"):
        with fitz.open() as pdf:
            for i in range(3):
                pdf.new_page().insert_text((50, 80), f"{name}: Evidence on page {i+1}")
            pdf.save(folder / f"{name}.pdf")
    state = client.app.state
    library = state.library_repository.register(state.file_inspector.inspect_root_directory(root), recursive=True, authorization_source="test")
    worker = Worker(state.task_queue, "graph-test", library_job_handlers(state.library_scanner))
    assert client.portal.call(worker.run_once)
    docs = client.get("/api/documents").json()["items"]
    with state.database.transaction() as connection:
        connection.execute("UPDATE contents SET page_count=3")
    return docs, library


def headers(auth):
    return {"Origin": "http://testserver", "X-LW-CSRF": auth.csrf}


def test_groups_validate_scope_and_conflict_without_affecting_documents(client, authenticated, tmp_path):
    docs, _ = seed(client, tmp_path)
    ids = [d["document_id"] for d in docs]
    h = headers(authenticated)
    response = client.post("/api/workspace/groups", headers=h, json={"title": "Methods", "document_ids": ids[:2]})
    assert response.status_code == 200
    group_id = response.json()["id"]
    payload = {"title": "Changed", "document_ids": ids, "revision": 0}
    assert client.patch(f"/api/workspace/groups/{group_id}", headers=h, json=payload).json()["revision"] == 1
    assert client.patch(f"/api/workspace/groups/{group_id}", headers=h, json=payload).status_code == 409
    assert client.get("/api/workspace/groups").json()["items"][0]["document_ids"] == ids
    for bad in ([], [str(uuid4()) for _ in range(9)]):
        assert client.post("/api/workspace/groups", headers=h, json={"title": "Bad", "document_ids": bad}).status_code == 422
    assert client.post("/api/workspace/groups", headers=h, json={"title": "Bad", "document_ids": [str(uuid4())]}).status_code == 404
    assert client.delete(f"/api/workspace/groups/{group_id}").status_code == 403
    assert client.delete(f"/api/workspace/groups/{group_id}", headers=h).status_code == 200
    assert client.get("/api/documents").json()["total"] == 3


def relation_payload(docs):
    return {"source_id": docs[0]["document_id"], "target_id": docs[1]["document_id"], "source_content_id": docs[0]["content_id"], "target_content_id": docs[1]["content_id"], "source_page": 2, "target_page": 3, "relation": "contradicts", "note": "不同采样条件，尚需核对"}


def test_relation_versions_pages_and_graph_provenance(client, authenticated, tmp_path):
    docs, _ = seed(client, tmp_path)
    h = headers(authenticated)
    payload = relation_payload(docs)
    assert client.post("/api/workspace/relations", headers=h, json={**payload, "target_page": 4}).status_code == 409
    assert client.post("/api/workspace/relations", headers=h, json={**payload, "source_content_id": "0" * 64}).json()["code"] == "source_changed"
    assert client.post("/api/workspace/relations", headers=h, json={**payload, "target_id": payload["source_id"]}).status_code == 422
    result = client.post("/api/workspace/relations", headers=h, json=payload)
    assert result.status_code == 200
    relation_id = result.json()["id"]
    client.patch(f"/api/workspace/reading/{docs[0]['document_id']}", headers=h, json={"tags": ["Methods"]})
    archive_answer(client.app.state.database, "Compare the designs", {"answer": "Fixture answer", "citations": [{"document_id": docs[1]["document_id"], "content_id": docs[1]["content_id"], "display_name": docs[1]["filename"], "physical_page": 2, "excerpt": "Original source fragment", "rects": []}]}, [d["document_id"] for d in docs[:2]])
    graph = client.get("/api/workspace/graph").json()
    assert {e["kind"] for e in graph["edges"]} == {"manual", "tag", "folder", "citation"}
    manual = next(e for e in graph["edges"] if e["kind"] == "manual")
    assert manual["relation"] == "contradicts" and manual["source_page"] == 2 and manual["target_page"] == 3
    assert manual["source_changed"] is False
    assert "verified" not in manual
    with client.app.state.database.transaction() as connection:
        connection.execute("UPDATE documents SET status='missing' WHERE id=?", (docs[1]["document_id"],))
    graph = client.get("/api/workspace/graph").json()
    assert all(e["source_changed"] for e in graph["edges"] if e["kind"] in ("manual", "citation"))
    assert next(e for e in graph["edges"] if e["kind"] == "citation")["citation"]["excerpt"] == "Original source fragment"
    assert next(n for n in graph["nodes"] if n.get("document_id") == docs[1]["document_id"])["available"] is False
    assert client.delete(f"/api/workspace/relations/{relation_id}").status_code == 403
    assert client.delete(f"/api/workspace/relations/{relation_id}", headers=h).status_code == 200
    assert client.get("/api/workspace/notebook").json()["items"][0]["answer"]["answer"] == "Fixture answer"


def test_disabled_libraries_do_not_appear_as_current_sources(client, authenticated, tmp_path):
    docs, library = seed(client, tmp_path)
    h = headers(authenticated)
    client.post("/api/workspace/groups", headers=h, json={"title": "Saved group", "document_ids": [docs[0]["document_id"]]})
    with client.app.state.database.transaction() as c:
        c.execute("UPDATE libraries SET status='disabled' WHERE id=?", (str(library.id),))
    graph = client.get("/api/workspace/graph").json()
    assert not any(n.get("available") for n in graph["nodes"])
    assert client.post("/api/workspace/relations", headers=h, json=relation_payload(docs)).status_code == 404
    client.cookies.clear()
    assert client.get("/api/workspace/graph").status_code == 403
    assert client.get("/api/workspace/groups").status_code == 403


def test_question_context_is_manual_scoped_and_preserves_versions(client, authenticated, tmp_path):
    docs, _ = seed(client, tmp_path)
    h = headers(authenticated)
    base = relation_payload(docs)
    context = {"question": "  Do methods change estimates?  ", "difference_basis": "method", "comparison_note": "A uses repeated sampling; B uses one visit."}
    response = client.post("/api/workspace/relations", headers=h, json={**base, **context})
    assert response.status_code == 200
    relation_id = response.json()["id"]
    assert client.post("/api/workspace/relations", headers=h, json={**base, **context, "difference_basis": "verified"}).status_code == 422
    assert client.post("/api/workspace/relations", headers=h, json={**base, "question": "x" * 501}).status_code == 422
    graph = client.get("/api/workspace/graph").json()
    manual = next(e for e in graph["edges"] if e["kind"] == "manual")
    assert manual["question"] == context["question"].strip()
    assert manual["difference_basis"] == "method"
    assert manual["comparison_note"] == context["comparison_note"]
    assert manual["source_content_id"] == base["source_content_id"]
    links = [e for e in graph["edges"] if e["kind"] == "question_link"]
    assert {e["target"] for e in links} == {"doc:" + d["document_id"] for d in docs[:2]}
    assert len({e["source"] for e in links}) == 1
    question = next(n for n in graph["nodes"] if n.get("question_text"))
    assert "entry_id" not in question
    # Identical question spelling/whitespace differences form one question node.
    assert client.post("/api/workspace/relations", headers=h, json={**base, **context, "question": "do  methods change estimates?"}).status_code == 200
    assert len([n for n in client.get("/api/workspace/graph").json()["nodes"] if n.get("question_text")]) == 1
    with client.app.state.database.transaction() as c:
        c.execute("UPDATE documents SET status='missing' WHERE id=?", (docs[1]["document_id"],))
    changed = client.get("/api/workspace/graph").json()
    assert all(e["source_changed"] for e in changed["edges"] if e["kind"] in ("manual", "question_link"))
    assert next(e for e in changed["edges"] if e["id"] == "manual:" + relation_id)["comparison_note"] == context["comparison_note"]
    assert client.delete(f"/api/workspace/relations/{relation_id}", headers=h).status_code == 200
    assert client.get("/api/documents").json()["total"] == 3
