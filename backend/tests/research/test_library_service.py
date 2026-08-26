from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.retriever import (
    LibraryRetrievalHit,
    RagRetriever,
)
from literature_workbench.features.research.service import (
    ResearchService,
    _build_library_prompt,
    _library_citations_for,
)
from literature_workbench.features.settings.repository import SettingsRepository

from conftest import make_page, publish_pages


@dataclass(slots=True)
class FakeDeepSeek:
    result: str = "Alpha means the first letter [d1 r1 p1]."
    error_sequence: list[str | None] | None = None
    result_sequence: list[str] | None = None
    calls: list[str] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)

    def answer_question(
        self,
        *,
        task_id: str,
        document_id: object,
        content_sha256: str | None,
        display_file_name: str | None,
        model: str,
        prompt: str,
        purpose: str = "research_question",
    ) -> str:
        self.calls.append(prompt)
        self.purposes.append(purpose)
        if self.error_sequence:
            outcome = self.error_sequence.pop(0)
            if outcome is not None:
                raise EgressRejected(outcome)
        if self.result_sequence:
            return self.result_sequence.pop(0)
        return self.result


@dataclass(slots=True)
class FakeQwenProcess:
    calls: int = 0

    def ensure_running(self) -> object:
        self.calls += 1
        return object()


@dataclass(slots=True)
class FakeQwen:
    result: str = "本地回答 [d1 r1 p1]"
    calls: list[str] = field(default_factory=list)

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.result


def _seed_document(
    database: Database,
    *,
    content_id: str,
    display_name: str,
    page_texts: list[list[str]] | None = None,
) -> str:
    """Seed one registered document with a completed parse run."""
    now = datetime.now(UTC).isoformat()
    document_id = str(uuid4())
    pages = [
        make_page(index + 1, texts)
        for index, texts in enumerate(page_texts or [["Alpha beta gamma"]])
    ]
    publish_pages(database, content_id, pages)
    with database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO libraries VALUES(?,?,?,?,?,1,'monitored',1,'test','active',?,?)",
            ("seed-library", "/library", "/library", "vol", "root", now, now),
        )
        connection.execute(
            """INSERT INTO documents(
                 id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                 mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                 auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                 VALUES(?,'seed-library',?,?,?,?,'pdf',12,1,'vol','file',1,'parse',1,
                        'registered_library','valid_pdf',?,?)""",
            (
                document_id,
                content_id,
                f"/library/{document_id}.pdf",
                f"/library/{document_id}.pdf",
                display_name,
                now,
                now,
            ),
        )
    return document_id


def _service(
    database: Database,
    deepseek: FakeDeepSeek,
    qwen: FakeQwen | None = None,
    qwen_process: FakeQwenProcess | None = None,
) -> ResearchService:
    repository = RagChunkRepository(database)
    return ResearchService(
        database,
        repository,
        RagRetriever(repository),
        SettingsRepository(database),
        deepseek,
        qwen or FakeQwen(),
        qwen_process or FakeQwenProcess(),
    )


def test_ask_library_indexes_parsed_documents_and_answers(
    research_db: Database,
) -> None:
    first = _seed_document(
        research_db,
        content_id="1" * 64,
        display_name="chevrotain.pdf",
        page_texts=[["Tragulus versicolor camera trap evidence"]],
    )
    _seed_document(
        research_db,
        content_id="2" * 64,
        display_name="conifer.pdf",
        page_texts=[["subalpine coniferous forest dynamics"]],
    )
    repository = RagChunkRepository(research_db)
    assert repository.indexed_content_count() == 0
    deepseek = FakeDeepSeek(result="Chevrotains were camera-trapped [d1 r1 p1].")

    answer = _service(research_db, deepseek).ask_library(
        question="What camera trap evidence exists?"
    )

    assert answer.provider == "deepseek"
    assert answer.indexed_documents == 2
    assert len(answer.citations) == 1
    assert answer.citation_status == "verified"
    citation = answer.citations[0]
    assert citation.document_id == first
    assert citation.display_name == "chevrotain.pdf"
    assert citation.physical_page == 1
    assert citation.rects == ((10, 20, 300, 80),)
    # The prompt carries the document list and the [dN rM pK] fragments.
    assert "d1: 《chevrotain.pdf》" in deepseek.calls[0]
    assert "[d1 r1 p1]" in deepseek.calls[0]
    assert "<documents>" in deepseek.calls[0]


def test_ask_library_sweep_reuses_existing_index(research_db: Database) -> None:
    _seed_document(research_db, content_id="3" * 64, display_name="paper.pdf")
    repository = RagChunkRepository(research_db)
    deepseek = FakeDeepSeek()
    service = _service(research_db, deepseek)

    service.ask_library(question="What is alpha?")
    indexed_run = repository.indexed_parse_run_id("3" * 64)
    assert indexed_run is not None

    service.ask_library(question="What is alpha?")
    assert repository.indexed_parse_run_id("3" * 64) == indexed_run
    assert repository.indexed_content_count() == 1


def test_library_citations_drop_bogus_marks_and_do_not_fall_back() -> None:
    hits = [
        _library_hit("doc-a", "a.pdf", 1, "First fragment text"),
        _library_hit("doc-b", "b.pdf", 2, "Second fragment text"),
    ]
    _, hits_by_ref = _build_library_prompt("q", hits)

    citations, status = _library_citations_for("答非所引 [d9 r1 p1] 还有 [d1 r9 p1]", hits_by_ref)

    # Invalid marks must NOT fall back to top hits.
    assert citations == []
    assert status == "missing"

    valid_citations, valid_status = _library_citations_for("有据可依 [d2 r1 p2]", hits_by_ref)
    assert [(c.document_id, c.physical_page) for c in valid_citations] == [("doc-b", 2)]
    assert valid_status == "verified"
    assert valid_citations[0].display_name == "b.pdf"
    assert "Second fragment" in valid_citations[0].excerpt


def test_library_citations_distinguishes_multiple_chunks_same_doc_and_page() -> None:
    hits = [
        _library_hit("doc-a", "a.pdf", 7, "Chunk 1 on page seven", rects=((10, 10, 50, 50),)),
        _library_hit("doc-a", "a.pdf", 7, "Chunk 2 on page seven", rects=((100, 100, 200, 200),)),
    ]
    _, hits_by_ref = _build_library_prompt("q", hits)

    # Model cites only the second fragment on page 7: [d1 r2 p7]
    citations, status = _library_citations_for("Only chunk 2 cited [d1 r2 p7]", hits_by_ref)
    assert len(citations) == 1
    assert status == "verified"
    assert citations[0].excerpt == "Chunk 2 on page seven"
    assert citations[0].rects == ((100, 100, 200, 200),)


def _library_hit(
    document_id: str,
    display_name: str,
    page: int,
    text: str,
    rects: tuple[tuple[int, int, int, int], ...] = ((10, 20, 300, 80),),
) -> LibraryRetrievalHit:
    return LibraryRetrievalHit(
        chunk_id=f"{document_id}:{page}:{uuid4()}",
        document_id=document_id,
        display_name=display_name,
        physical_page=page,
        text=text,
        score=1.0,
        rects=rects,
    )


def test_ask_library_expands_cjk_question_through_deepseek(
    research_db: Database,
) -> None:
    _seed_document(
        research_db,
        content_id="4" * 64,
        display_name="paper.pdf",
        page_texts=[["Alpha beta gamma delta"]],
    )
    deepseek = FakeDeepSeek(
        result_sequence=["alpha, beta", "阿尔法就是 alpha [d1 r1 p1]"]
    )

    answer = _service(research_db, deepseek).ask_library(question="阿尔法是什么？")

    assert answer.provider == "deepseek"
    assert answer.answer == "阿尔法就是 alpha [d1 r1 p1]"
    assert deepseek.purposes == ["research_query_expansion", "research_question"]
    assert "阿尔法是什么？" in deepseek.calls[0]
    assert "[d1 r1 p1]" in deepseek.calls[1]
    assert [c.display_name for c in answer.citations] == ["paper.pdf"]


def test_ask_library_overview_falls_back_to_abstract_keywords(
    research_db: Database,
) -> None:
    _seed_document(
        research_db,
        content_id="5" * 64,
        display_name="paper.pdf",
        page_texts=[["Abstract: we study alpha particles in detail"]],
    )
    deepseek = FakeDeepSeek(
        result_sequence=["OVERVIEW", "库里都在研究 alpha [d1 r1 p1]"]
    )

    answer = _service(research_db, deepseek).ask_library(question="文献库总览")

    assert answer.provider == "deepseek"
    assert deepseek.purposes == ["research_query_expansion", "research_question"]
    # The abstract-keyword fallback retrieved the fragment even though the
    # CJK question has no Latin tokens.
    assert "[d1 r1 p1]" in deepseek.calls[1]
    assert "Abstract" in deepseek.calls[1]


def test_ask_library_uses_qwen_directly_in_local_only_mode(
    research_db: Database,
) -> None:
    _seed_document(research_db, content_id="6" * 64, display_name="paper.pdf")
    SettingsRepository(research_db).update(default_translation_mode="local_only")
    deepseek = FakeDeepSeek()
    qwen = FakeQwen(result="本地回答 [d1 r1 p1]")
    qwen_process = FakeQwenProcess()

    answer = _service(research_db, deepseek, qwen, qwen_process).ask_library(
        question="What is alpha?"
    )

    assert answer.provider == "qwen"
    assert answer.model == "qwen-local"
    assert answer.fallback_reason is None
    assert answer.indexed_documents == 1
    assert deepseek.calls == []
    assert qwen_process.calls == 1
    assert answer.citations[0].display_name == "paper.pdf"


def test_ask_library_falls_back_to_qwen_when_deepseek_fails(
    research_db: Database,
) -> None:
    _seed_document(research_db, content_id="7" * 64, display_name="paper.pdf")
    SettingsRepository(research_db).update(qwen_fallback_enabled=True)
    deepseek = FakeDeepSeek(
        error_sequence=["deepseek_transport_failed", "deepseek_transport_failed"]
    )
    qwen = FakeQwen(result="本地回答 [d1 r1 p1]")

    answer = _service(research_db, deepseek, qwen).ask_library(
        question="What is alpha?"
    )

    assert answer.provider == "qwen"
    assert answer.fallback_reason == "deepseek_transport_failed"
    assert len(deepseek.calls) == 2


def test_ask_library_without_hits_returns_canned_answer(
    research_db: Database,
) -> None:
    _seed_document(
        research_db,
        content_id="8" * 64,
        display_name="paper.pdf",
        page_texts=[["Alpha beta gamma"]],
    )
    deepseek = FakeDeepSeek()
    qwen = FakeQwen()

    answer = _service(research_db, deepseek, qwen).ask_library(
        question="What is quantum entanglement?"
    )

    assert answer.provider == "retrieval_only"
    assert answer.citations == []
    assert answer.citation_status == "missing"
    assert answer.indexed_documents == 1
    assert deepseek.calls == []
    assert qwen.calls == []


def test_ask_library_empty_library_reports_zero_coverage(
    research_db: Database,
) -> None:
    deepseek = FakeDeepSeek()

    answer = _service(research_db, deepseek).ask_library(question="What is alpha?")

    assert answer.provider == "retrieval_only"
    assert answer.indexed_documents == 0
    assert answer.citation_status == "missing"
    assert deepseek.calls == []
