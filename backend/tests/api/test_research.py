from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi.testclient import TestClient

from literature_workbench.features.research.service import (
    ResearchAnswer,
    ResearchCitation,
)


def _headers(authenticated) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-LW-CSRF": authenticated.csrf}


def test_cached_study_browsing_requires_session_and_never_creates_records(client, authenticated):
    root = '/api/libraries/full-text/studies'
    assert client.get(root).json() == {'items': []}
    assert client.get(f'{root}/{uuid4()}').status_code == 404
    assert client.get(f'{root}/invalid').status_code == 422
    assert client.get(f'{root}/{uuid4()}?offset=-1').status_code == 422
    with client.app.state.database.connect() as c:
        assert c.execute('SELECT count(*) FROM full_text_study_cache').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM research_entries').fetchone()[0] == 0
    client.cookies.clear()
    assert client.get(root).status_code == 403


@dataclass(slots=True)
class StubResearchService:
    answer: ResearchAnswer | None = None
    error: str | None = None
    calls: list[tuple[str, str]] | None = None

    def ask(self, *, document_id, question: str) -> ResearchAnswer:
        if self.calls is not None:
            self.calls.append((str(document_id), question))
        if self.error is not None:
            from literature_workbench.features.research.service import ResearchError

            raise ResearchError(self.error)
        assert self.answer is not None
        return self.answer


def _answer() -> ResearchAnswer:
    return ResearchAnswer(
        answer="Alpha is the first letter [r1 p1].",
        citations=[
            ResearchCitation(
                physical_page=1,
                excerpt="Alpha beta gamma",
                rects=((10, 20, 300, 80),),
            )
        ],
        provider="deepseek",
        model="deepseek-v4-flash",
        citation_status="verified",
    )


def test_ask_returns_answer_body(client: TestClient, authenticated) -> None:
    stub = StubResearchService(answer=_answer(), calls=[])
    client.app.state.research_service = stub
    document_id = str(uuid4())

    response = client.post(
        f"/api/documents/{document_id}/ask",
        headers=_headers(authenticated),
        json={"question": "What is alpha?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Alpha is the first letter [r1 p1]."
    assert body["citations"] == [
        {"physical_page": 1, "excerpt": "Alpha beta gamma", "rects": [[10, 20, 300, 80]], "document_id": document_id, "display_name": "原文献", "content_id": None}
    ]
    assert body["provider"] == "deepseek"
    assert body["model"] == "deepseek-v4-flash"
    assert body["fallback_reason"] is None
    assert body["citation_status"] == "verified"
    assert stub.calls == [(document_id, "What is alpha?")]


def test_ask_enforces_bounded_question_contract(client: TestClient, authenticated) -> None:
    document_id = str(uuid4())
    url = f"/api/documents/{document_id}/ask"

    extra = client.post(
        url,
        headers=_headers(authenticated),
        json={"question": "What is alpha?", "url": "https://evil.test"},
    )
    empty = client.post(url, headers=_headers(authenticated), json={"question": ""})
    oversized = client.post(
        url, headers=_headers(authenticated), json={"question": "a" * 2001}
    )

    assert extra.status_code == 422
    assert empty.status_code == 422
    assert oversized.status_code == 422


def test_selection_reading_verifies_source_and_archives_the_original_context(client, authenticated) -> None:
    from datetime import UTC, datetime

    document_id = _register_parsed_document(client)
    version = str(uuid4())
    with client.app.state.database.transaction() as c:
        c.execute("INSERT INTO document_versions VALUES(?,?,?,1,1,'v','f',1,?)",
                  (version, document_id, "c" * 64, datetime.now(UTC).isoformat()))
    calls = []

    class Model:
        def answer_question(self, **kwargs):
            calls.append(kwargs)
            return "选区含义来自原文 [r1 p1]。"

    client.app.state.research_service._deepseek = Model()
    selection = {"document_version_id": version, "content_id": "c" * 64,
                 "physical_page": 1, "selected_text": "Alpha beta gamma"}
    payload = {"question": "请解释选区的方法假设", "source_selection": selection}
    response = client.post(f"/api/documents/{document_id}/ask", headers=_headers(authenticated), json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["citations"][0]["excerpt"] == "Alpha beta gamma"
    assert body["citations"][0]["physical_page"] == 1
    assert body["selection_context"]["content_id"] == "c" * 64
    assert len(calls) == 1  # The verified passage needs no paid query expansion.
    assert "[r1 p1]" in calls[0]["prompt"] and "Alpha beta gamma" in calls[0]["prompt"]
    assert "untrusted" in calls[0]["system_prompt"]
    archived = client.get("/api/workspace/notebook").json()["items"][0]
    assert archived["answer"]["selection_context"] == body["selection_context"]
    for route, changed in [
        (document_id, {**selection, "selected_text": "invented statement"}),
        (document_id, {**selection, "document_version_id": str(uuid4())}),
        (str(uuid4()), selection),
    ]:
        rejected = client.post(f"/api/documents/{route}/ask", headers=_headers(authenticated),
                               json={**payload, "source_selection": changed})
        assert rejected.status_code == 409
    assert len(calls) == 1
    library = client.post("/api/libraries/ask", headers=_headers(authenticated), json=payload)
    assert library.status_code == 422
    assert len(calls) == 1


def test_ask_maps_service_error_codes(client: TestClient, authenticated) -> None:
    cases = {
        "document_not_found": 404,
        "parse_required": 409,
        "question_empty": 400,
        "deepseek_total_timeout": 502,
    }
    for code, expected_status in cases.items():
        client.app.state.research_service = StubResearchService(error=code)
        response = client.post(
            f"/api/documents/{uuid4()}/ask",
            headers=_headers(authenticated),
            json={"question": "What is alpha?"},
        )
        assert response.status_code == expected_status, code
        assert response.json() == {"code": code}


def test_ask_unknown_document_returns_404_with_real_service(
    client: TestClient, authenticated
) -> None:
    response = client.post(
        f"/api/documents/{uuid4()}/ask",
        headers=_headers(authenticated),
        json={"question": "What is alpha?"},
    )

    assert response.status_code == 404
    assert response.json() == {"code": "document_not_found"}


def test_ask_without_retrieval_hits_returns_canned_answer(
    client: TestClient, authenticated
) -> None:
    """Real service end-to-end on a seeded document: no hits, no provider call."""
    now_ids = _register_parsed_document(client)
    response = client.post(
        f"/api/documents/{now_ids}/ask",
        headers=_headers(authenticated),
        json={"question": "What is quantum entanglement?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "retrieval_only"
    assert body["citations"] == []
    assert body["citation_status"] == "missing"
    assert body["answer"]


def _register_parsed_document(client: TestClient) -> str:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    ids = {
        "library": str(uuid4()),
        "document": str(uuid4()),
        "content": "c" * 64,
        "run": str(uuid4()),
    }
    with client.app.state.database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries VALUES(?,?,?,?,?,1,'monitored',1,'test','active',?,?)",
            (ids["library"], "/library", "/library", "v", "r", now, now),
        )
        connection.execute(
            """INSERT INTO contents(
                 sha256,size_bytes,mime_type,page_count,current_parse_run_id,status,
                 created_at,updated_at) VALUES(?,1,'application/pdf',1,NULL,'completed',?,?)""",
            (ids["content"], now, now),
        )
        connection.execute(
            """INSERT INTO documents(
                 id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                 mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                 auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,'paper.pdf','pdf',1,1,'v','f',1,'parse',1,
                        'registered_library','valid_pdf',?,?)""",
            (
                ids["document"],
                ids["library"],
                ids["content"],
                "/library/paper.pdf",
                "/library/paper.pdf",
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO parse_runs(
                 id,content_sha256,parser_name,parser_version,status,created_at,updated_at,
                 completed_at) VALUES(?,?,'pymupdf','1','completed',?,?,?)""",
            (ids["run"], ids["content"], now, now, now),
        )
        connection.execute(
            "UPDATE contents SET current_parse_run_id=? WHERE sha256=?",
            (ids["run"], ids["content"]),
        )
        connection.execute(
            "INSERT INTO pages VALUES(?,0,1,?,'Alpha beta gamma','Alpha beta gamma',612,792,'[]',1)",
            (ids["content"], ids["run"]),
        )
    return ids["document"]


def test_library_ask_returns_attributed_citations(
    client: TestClient, authenticated
) -> None:
    from literature_workbench.features.research.service import (
        LibraryAnswer,
        LibraryCitation,
    )

    @dataclass(slots=True)
    class StubLibraryService:
        calls: list[str] | None = None

        def ask_library(self, *, question: str) -> LibraryAnswer:
            if self.calls is not None:
                self.calls.append(question)
            return LibraryAnswer(
                answer="库里有alpha [d1 r1 p1]",
                citations=[
                    LibraryCitation(
                        document_id="doc-1",
                        display_name="paper.pdf",
                        physical_page=1,
                        excerpt="Alpha beta gamma",
                        rects=((10, 20, 300, 80),),
                    )
                ],
                provider="deepseek",
                model="deepseek-v4-flash",
                indexed_documents=3,
                citation_status="verified",
            )

    stub = StubLibraryService(calls=[])
    client.app.state.research_service = stub

    response = client.post(
        "/api/libraries/ask",
        headers=_headers(authenticated),
        json={"question": "库里有alpha吗？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "库里有alpha [d1 r1 p1]"
    assert body["citations"] == [
        {
            "document_id": "doc-1",
            "content_id": None,
            "display_name": "paper.pdf",
            "physical_page": 1,
            "excerpt": "Alpha beta gamma",
            "rects": [[10, 20, 300, 80]],
        }
    ]
    assert body["provider"] == "deepseek"
    assert body["fallback_reason"] is None
    assert body["indexed_documents"] == 3
    assert body["citation_status"] == "verified"
    assert stub.calls == ["库里有alpha吗？"]


def test_library_ask_empty_library_returns_canned_answer(
    client: TestClient, authenticated
) -> None:
    # The real service on a fresh database: nothing parsed, nothing indexed.
    response = client.post(
        "/api/libraries/ask",
        headers=_headers(authenticated),
        json={"question": "What is alpha?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "retrieval_only"
    assert body["citations"] == []
    assert body["citation_status"] == "missing"
    assert body["indexed_documents"] == 0


def test_library_ask_enforces_question_contract(
    client: TestClient, authenticated
) -> None:
    response = client.post(
        "/api/libraries/ask",
        headers=_headers(authenticated),
        json={"question": "x" * 2001},
    )
    assert response.status_code == 422


def test_full_text_api_preflight_requires_explicit_scope_and_archives_only_verified_quotes(client, authenticated):
    import json
    document_id = _register_parsed_document(client)
    calls = []
    class Model:
        def answer_question(self, **kwargs):
            calls.append(kwargs)
            return json.dumps({"claims": [{"conclusion": "A bounded finding", "evidence": [{"ref": "[d1 p1]", "quote": "Alpha beta gamma"}]}], "limitations": []})
    client.app.state.research_service._deepseek = Model()
    payload = {"question": "Compare", "document_ids": [document_id]}
    prepare = client.post("/api/libraries/full-text/prepare", headers=_headers(authenticated), json=payload)
    assert prepare.status_code == 200 and not calls
    assert client.get("/api/workspace/notebook").json()["items"] == []
    rejected = client.post("/api/libraries/ask", headers=_headers(authenticated), json={**payload, "reading_mode": "full_text"})
    assert rejected.status_code == 409 and not calls
    result = client.post("/api/libraries/ask", headers=_headers(authenticated), json={**payload, "reading_mode": "full_text", "source_versions": prepare.json()["source_versions"], "plan_id": prepare.json()["plan_id"]})
    assert result.status_code == 200 and len(calls) == 1
    body = result.json()
    assert body["reading_coverage"]["coverage"][0]["supplied_pages"] == 1
    assert body["citations"][0]["excerpt"] == "Alpha beta gamma"
    assert body["history_saved"]
    saved = client.get("/api/workspace/notebook").json()["items"][0]
    assert saved["answer"]["reading_coverage"] == body["reading_coverage"]
    assert saved["answer"]["citations"] == [{**item, "source_changed": False} for item in body["citations"]]
    no_scope = client.post("/api/libraries/ask", headers=_headers(authenticated), json={"question": "Compare", "reading_mode": "full_text"})
    assert no_scope.status_code == 400 and len(calls) == 1


def test_full_text_forged_quote_is_not_archived(client, authenticated):
    doc = _register_parsed_document(client)
    class Model:
        def answer_question(self, **kwargs):
            return '{"claims":[{"conclusion":"fake","evidence":[{"ref":"[d1 p1]","quote":"Never present in original"}]}],"limitations":[]}'
    client.app.state.research_service._deepseek = Model()
    payload = {"question": "Compare", "document_ids": [doc]}
    ready = client.post("/api/libraries/full-text/prepare", headers=_headers(authenticated), json=payload).json()
    response = client.post("/api/libraries/ask", headers=_headers(authenticated), json={**payload, "reading_mode": "full_text", "source_versions": ready["source_versions"], "plan_id": ready["plan_id"]})
    assert response.status_code == 502 and response.json()["code"] == "full_text_unverified_answer"
    assert client.get("/api/workspace/notebook").json()["items"] == []
