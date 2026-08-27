from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.retriever import RagRetriever
from literature_workbench.features.research.service import (
    ResearchError,
    ResearchService,
)
from literature_workbench.features.settings.repository import SettingsRepository

from conftest import make_page, publish_pages


@dataclass(slots=True)
class FakeDeepSeek:
    result: str = "Alpha means the first letter [r1 p1]."
    error_sequence: list[str | None] | None = None
    result_sequence: list[str] | None = None
    calls: list[str] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)

    def answer_question(
        self,
        *,
        task_id: str,
        document_id: object,
        content_sha256: str,
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
    result: str = "本地回答 [r1 p1]"
    calls: list[str] = field(default_factory=list)

    def translate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.result


def _seed_document(
    database: Database,
    *,
    content_id: str,
    page_texts: list[list[str]] | None = None,
    publish: bool = True,
    status: str = "valid_pdf",
    library_status: str = "active",
) -> str:
    """Seed library + document; optionally publish a parse run with pages."""
    now = datetime.now(UTC).isoformat()
    document_id = str(uuid4())
    library_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO libraries VALUES(?,?,?,?,?,1,'monitored',1,'test',?,?,?)",
            (library_id, f"/library_{library_id}", f"/library_{library_id}", "vol", "root", library_status, now, now),
        )
    if publish:
        pages = [
            make_page(index + 1, texts)
            for index, texts in enumerate(page_texts or [["Alpha beta gamma"]])
        ]
        publish_pages(database, content_id, pages)
    else:
        # No parse run yet, but the document still needs its contents row.
        with database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO contents(
                     sha256,size_bytes,mime_type,page_count,status,created_at,updated_at)
                   VALUES(?,12,'application/pdf',1,'valid_pdf',?,?)""",
                (content_id, now, now),
            )
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO documents(
                 id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                 mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                 auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,'paper.pdf','pdf',12,1,'vol','file',1,'parse',1,
                        'registered_library',?,?,?)""",
            (
                document_id,
                library_id,
                content_id,
                f"/library/{document_id}.pdf",
                f"/library/{document_id}.pdf",
                status,
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


def test_ask_returns_deepseek_answer_with_page_citations(research_db: Database) -> None:
    document_id = _seed_document(
        research_db,
        content_id="d" * 64,
        page_texts=[["Alpha beta gamma delta"], ["Epsilon zeta eta theta"]],
    )
    deepseek = FakeDeepSeek(result="Alpha is on page one [r1 p1].")

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert answer.provider == "deepseek"
    assert answer.model == "deepseek-v4-flash"
    assert answer.fallback_reason is None
    assert answer.citation_status == "verified"
    assert [citation.physical_page for citation in answer.citations] == [1]
    assert "Alpha" in answer.citations[0].excerpt
    assert answer.citations[0].rects == ((10, 20, 300, 80),)
    assert len(deepseek.calls) == 1
    assert "[r1 p1]" in deepseek.calls[0]
    assert "<question>" in deepseek.calls[0]


def test_ask_distinguishes_multiple_chunks_on_same_page(research_db: Database) -> None:
    # Page 1 contains two long paragraphs that exceed max_chars and form two separate chunks on page 1
    long_para_1 = "Alpha discovery details: " + ("scientific background " * 75)
    long_para_2 = "Alpha conclusion summary: " + ("verified experimentally " * 75)
    document_id = _seed_document(
        research_db,
        content_id="c" * 64,
        page_texts=[[long_para_1, long_para_2]],
    )
    # Model cites only fragment 2 on page 1: [r2 p1]
    deepseek = FakeDeepSeek(result="According to the study [r2 p1], the conclusion was verified.")

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert len(answer.citations) == 1
    assert answer.citation_status == "verified"
    assert answer.citations[0].physical_page == 1
    # Must match the second chunk's excerpt, not the first chunk
    assert "conclusion" in answer.citations[0].excerpt


def test_ask_returns_empty_citations_when_model_does_not_cite(research_db: Database) -> None:
    document_id = _seed_document(
        research_db,
        content_id="e0" * 32,
        page_texts=[["Alpha beta gamma delta"]],
    )
    # Model returns answer with NO citation marks at all
    deepseek = FakeDeepSeek(result="Alpha is a well-known scientific concept.")

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert answer.provider == "deepseek"
    assert answer.citations == []
    assert answer.citation_status == "missing"


def test_ask_ignores_ambiguous_legacy_citation_when_page_has_multiple_hits(
    research_db: Database,
) -> None:
    long_para_1 = "Alpha discovery details: " + ("scientific background " * 75)
    long_para_2 = "Alpha conclusion summary: " + ("verified experimentally " * 75)
    document_id = _seed_document(
        research_db,
        content_id="a1" * 32,
        page_texts=[[long_para_1, long_para_2]],
    )
    # Model returns legacy ambiguous [p1] when page 1 contains two distinct hits
    deepseek = FakeDeepSeek(result="Alpha was discovered in the study [p1].")

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="What is alpha?"
    )

    # Must be treated as ambiguous and NOT guessed
    assert answer.citations == []
    assert answer.citation_status == "missing"


def test_ask_accepts_unique_legacy_citation_when_page_has_single_hit(
    research_db: Database,
) -> None:
    document_id = _seed_document(
        research_db,
        content_id="a2" * 32,
        page_texts=[["Alpha discovery unique single paragraph on page one."]],
    )
    # Model returns legacy [p1] when page 1 only has a single hit
    deepseek = FakeDeepSeek(result="Alpha is described in [p1].")

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert len(answer.citations) == 1
    assert answer.citation_status == "verified"
    assert answer.citations[0].physical_page == 1


def test_ask_rejects_missing_or_inactive_document(research_db: Database) -> None:
    missing_doc = _seed_document(
        research_db,
        content_id="m" * 64,
        status="missing",
    )
    with pytest.raises(ResearchError, match="document_not_found"):
        _service(research_db, FakeDeepSeek()).ask(
            document_id=missing_doc, question="What is alpha?"
        )

    inactive_lib_doc = _seed_document(
        research_db,
        content_id="i" * 64,
        library_status="inactive",
    )
    with pytest.raises(ResearchError, match="document_not_found"):
        _service(research_db, FakeDeepSeek()).ask(
            document_id=inactive_lib_doc, question="What is alpha?"
        )


def test_ask_retries_once_on_transient_deepseek_error(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="e" * 64)
    deepseek = FakeDeepSeek(error_sequence=["deepseek_total_timeout", None])

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert answer.provider == "deepseek"
    assert len(deepseek.calls) == 2


def test_ask_falls_back_to_qwen_when_enabled_and_deepseek_fails(
    research_db: Database,
) -> None:
    document_id = _seed_document(research_db, content_id="f" * 64)
    SettingsRepository(research_db).update(qwen_fallback_enabled=True)
    deepseek = FakeDeepSeek(
        error_sequence=["deepseek_transport_failed", "deepseek_transport_failed"]
    )
    qwen = FakeQwen(result="本地回答 [r1 p1]")
    qwen_process = FakeQwenProcess()

    answer = _service(research_db, deepseek, qwen, qwen_process).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert answer.provider == "qwen"
    assert answer.fallback_reason == "deepseek_transport_failed"
    assert len(deepseek.calls) == 2
    assert qwen_process.calls == 1
    assert len(qwen.calls) == 1


def test_ask_surfaces_deepseek_code_when_fallback_disabled(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="1" * 64)
    deepseek = FakeDeepSeek(error_sequence=["deepseek_key_unavailable"])

    with pytest.raises(ResearchError, match="deepseek_key_unavailable"):
        _service(research_db, deepseek).ask(
            document_id=document_id, question="What is alpha?"
        )


def test_ask_uses_qwen_directly_in_local_only_mode(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="2" * 64)
    SettingsRepository(research_db).update(default_translation_mode="local_only")
    deepseek = FakeDeepSeek()
    qwen = FakeQwen()

    answer = _service(research_db, deepseek, qwen).ask(
        document_id=document_id, question="What is alpha?"
    )

    assert answer.provider == "qwen"
    assert answer.fallback_reason is None
    assert deepseek.calls == []


def test_ask_without_hits_returns_canned_answer_without_provider_call(
    research_db: Database,
) -> None:
    document_id = _seed_document(research_db, content_id="3" * 64)
    deepseek = FakeDeepSeek()
    qwen = FakeQwen()

    answer = _service(research_db, deepseek, qwen).ask(
        document_id=document_id, question="What is quantum entanglement?"
    )

    assert answer.provider == "retrieval_only"
    assert answer.citations == []
    assert answer.citation_status == "missing"
    assert answer.answer
    assert deepseek.calls == []
    assert qwen.calls == []


def test_ask_rejects_unknown_document(research_db: Database) -> None:
    with pytest.raises(ResearchError, match="document_not_found"):
        _service(research_db, FakeDeepSeek()).ask(
            document_id=uuid4(), question="What is alpha?"
        )


def test_ask_requires_completed_parse(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="4" * 64, publish=False)

    with pytest.raises(ResearchError, match="parse_required"):
        _service(research_db, FakeDeepSeek()).ask(
            document_id=document_id, question="What is alpha?"
        )


def test_ask_validates_question_text(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="5" * 64)
    service = _service(research_db, FakeDeepSeek())

    with pytest.raises(ResearchError, match="question_empty"):
        service.ask(document_id=document_id, question="   ")
    with pytest.raises(ResearchError, match="question_too_long"):
        service.ask(document_id=document_id, question="a" * 2001)


def test_ask_builds_index_lazily_and_reuses_it(research_db: Database) -> None:
    content_id = "6" * 64
    document_id = _seed_document(research_db, content_id=content_id)
    repository = RagChunkRepository(research_db)
    assert repository.indexed_parse_run_id(content_id) is None
    deepseek = FakeDeepSeek()
    service = _service(research_db, deepseek)

    service.ask(document_id=document_id, question="What is alpha?")
    indexed_run = repository.indexed_parse_run_id(content_id)
    assert indexed_run is not None

    service.ask(document_id=document_id, question="What is alpha?")
    assert repository.indexed_parse_run_id(content_id) == indexed_run
    assert len(deepseek.calls) == 2


def test_ask_expands_cjk_question_through_deepseek(research_db: Database) -> None:
    document_id = _seed_document(
        research_db,
        content_id="7" * 64,
        page_texts=[["Alpha beta gamma delta"], ["Epsilon zeta eta theta"]],
    )
    deepseek = FakeDeepSeek(
        result_sequence=["alpha, beta", "阿尔法就是 alpha [r1 p1]"]
    )

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="阿尔法是什么意思？"
    )

    assert answer.provider == "deepseek"
    assert answer.answer == "阿尔法就是 alpha [r1 p1]"
    assert [citation.physical_page for citation in answer.citations] == [1]
    assert deepseek.purposes == ["research_query_expansion", "research_question"]
    assert "阿尔法是什么意思？" in deepseek.calls[0]
    assert "[r1 p1]" in deepseek.calls[1]


def test_ask_uses_leading_chunks_for_overview_questions(research_db: Database) -> None:
    document_id = _seed_document(
        research_db,
        content_id="8" * 64,
        page_texts=[["Alpha beta gamma delta"], ["Epsilon zeta eta theta"]],
    )
    deepseek = FakeDeepSeek(
        result_sequence=["OVERVIEW", "本文研究了 alpha [r1 p1]"]
    )

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="这篇文献的主题是什么？"
    )

    assert answer.provider == "deepseek"
    assert deepseek.purposes == ["research_query_expansion", "research_question"]
    assert "[r1 p1]" in deepseek.calls[1]
    assert "Alpha beta" in deepseek.calls[1]
    assert [citation.physical_page for citation in answer.citations] == [1]


def test_ask_canned_when_expansion_unusable(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="9" * 64)
    deepseek = FakeDeepSeek(result_sequence=["我无法理解这个问题"])

    answer = _service(research_db, deepseek).ask(
        document_id=document_id, question="这个问题怎么回答？"
    )

    assert answer.provider == "retrieval_only"
    assert deepseek.purposes == ["research_query_expansion"]


def test_ask_surfaces_expansion_deepseek_failure(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="a" * 64)
    deepseek = FakeDeepSeek(error_sequence=["deepseek_key_unavailable"])

    with pytest.raises(ResearchError, match="deepseek_key_unavailable"):
        _service(research_db, deepseek).ask(
            document_id=document_id, question="阿尔法是什么意思？"
        )
    assert deepseek.purposes == ["research_query_expansion"]


def test_ask_skips_expansion_in_local_only_mode(research_db: Database) -> None:
    document_id = _seed_document(research_db, content_id="b" * 64)
    SettingsRepository(research_db).update(default_translation_mode="local_only")
    deepseek = FakeDeepSeek()
    qwen = FakeQwen()

    answer = _service(research_db, deepseek, qwen).ask(
        document_id=document_id, question="阿尔法是什么意思？"
    )

    assert answer.provider == "retrieval_only"
    assert deepseek.calls == []
    assert qwen.calls == []
