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
        {"physical_page": 1, "excerpt": "Alpha beta gamma", "rects": [[10, 20, 300, 80]]}
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
