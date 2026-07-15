from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient


def _headers(authenticated) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf}


def _register_page(client: TestClient) -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    ids = {"library": str(uuid4()), "document": str(uuid4()), "version": str(uuid4()),
           "content": "b" * 64, "run": str(uuid4())}
    with client.app.state.database.transaction() as connection:
        connection.execute("INSERT INTO libraries VALUES(?,?,?,?,?,1,'monitored',1,'test','active',?,?)",
                           (ids["library"], "/library", "/library", "v", "r", now, now))
        connection.execute("INSERT INTO contents(sha256,size_bytes,mime_type,page_count,current_parse_run_id,status,created_at,updated_at) VALUES(?,1,'application/pdf',1,NULL,'completed',?,?)",
                           (ids["content"], now, now))
        connection.execute("""INSERT INTO documents(id,library_id,content_sha256,path,path_key,
            display_name,format,size_bytes,mtime_ns,volume_id,file_id,hard_link_count,
            parse_preference,auto_cloud_eligible,authorization_source,status,created_at,updated_at)
            VALUES(?,?,?,?,?,'paper.pdf','pdf',1,1,'v','f',1,'parse',1,'registered_library','valid_pdf',?,?)""",
            (ids["document"], ids["library"], ids["content"], "/library/paper.pdf", "/library/paper.pdf", now, now))
        connection.execute("INSERT INTO document_versions VALUES(?,?,?,1,1,'v','f',1,?)",
                           (ids["version"], ids["document"], ids["content"], now))
        connection.execute("INSERT INTO parse_runs(id,content_sha256,parser_name,parser_version,status,created_at,updated_at,completed_at) VALUES(?,?,'pymupdf','1','completed',?,?,?)",
                           (ids["run"], ids["content"], now, now, now))
        connection.execute("UPDATE contents SET current_parse_run_id=? WHERE sha256=?",
                           (ids["run"], ids["content"]))
        connection.execute("INSERT INTO pages VALUES(?,0,1,?,'selected text','selected text',612,792,'[]',1)",
                           (ids["content"], ids["run"]))
    return ids


def _payload(ids: dict[str, str]) -> dict[str, object]:
    return {"client_task_id": str(uuid4()), "document_id": ids["document"],
            "document_version_id": ids["version"], "content_id": ids["content"],
            "physical_page": 1, "selected_text": "selected text", "target_language": "zh-CN"}


def test_translation_api_accepts_only_bounded_selection_contract(client, authenticated) -> None:
    ids = _register_page(client)
    bad = _payload(ids) | {"context_before": "caller context", "url": "https://evil.test"}
    rejected = client.post("/api/translations", headers=_headers(authenticated), json=bad)
    created = client.post("/api/translations", headers=_headers(authenticated), json=_payload(ids))

    assert rejected.status_code == 422
    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    task_id = created.json()["id"]
    fetched = client.get(f"/api/translations/{task_id}")
    assert fetched.status_code == 200
    assert fetched.json()["selected_text"] == "selected text"


def test_translation_api_rejects_wrong_target_and_forged_text_without_task(client, authenticated) -> None:
    ids = _register_page(client)
    with client.app.state.database.connect() as connection:
        before = connection.execute("SELECT count(*) FROM translation_tasks").fetchone()[0]
    wrong = _payload(ids) | {"target_language": "en-US"}
    forged = _payload(ids) | {"selected_text": "forged secret"}

    assert client.post("/api/translations", headers=_headers(authenticated), json=wrong).status_code == 422
    response = client.post("/api/translations", headers=_headers(authenticated), json=forged)
    assert response.status_code == 409
    assert response.json() == {"code": "selection_not_current"}
    with client.app.state.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM translation_tasks").fetchone()[0] == before


def test_translation_get_hides_unknown_task(client, authenticated) -> None:
    response = client.get(f"/api/translations/{uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"code": "translation_not_found"}


def test_translation_get_hides_task_from_previous_session(client, authenticated) -> None:
    ids = _register_page(client)
    created = client.post(
        "/api/translations", headers=_headers(authenticated), json=_payload(ids)
    )
    assert created.status_code == 202

    client.app.state.local_session.session_generation = str(uuid4())
    response = client.get(f"/api/translations/{created.json()['id']}")

    assert response.status_code == 404
    assert response.json() == {"code": "translation_not_found"}
