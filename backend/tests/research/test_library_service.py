from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from literature_workbench.features.research.relation_analysis import parse_analysis

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
    system_prompts: list[str | None] = field(default_factory=list)
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
        system_prompt: str | None = None,
        purpose: str = "research_question",
    ) -> str:
        self.calls.append(prompt)
        self.system_prompts.append(system_prompt)
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
    *_, hits_by_ref = _build_library_prompt("q", hits)

    citations, status = _library_citations_for("答非所引 [d9 r1 p1] 还有 [d1 r9 p1]", hits_by_ref)

    # Invalid marks must NOT fall back to top hits.
    assert citations == []
    assert status == "missing"

    valid_citations, valid_status = _library_citations_for("有据可依 [d2 r1 p2]", hits_by_ref)
    assert [(c.document_id, c.physical_page) for c in valid_citations] == [("doc-b", 2)]
    assert valid_status == "verified"
    assert valid_citations[0].display_name == "b.pdf"
    assert "Second fragment" in valid_citations[0].excerpt


def test_library_citations_ignore_ambiguous_legacy_citation_when_doc_page_has_multiple_hits() -> None:
    hits = [
        _library_hit("doc-a", "a.pdf", 1, "First fragment text on page 1"),
        _library_hit("doc-a", "a.pdf", 1, "Second fragment text on page 1"),
    ]
    *_, hits_by_ref = _build_library_prompt("q", hits)

    # Model uses legacy ambiguous [d1 p1] when doc 1 page 1 has two chunks
    citations, status = _library_citations_for("参考了 [d1 p1]", hits_by_ref)

    assert citations == []
    assert status == "missing"


def test_library_citations_accept_unique_legacy_citation() -> None:
    hits = [
        _library_hit("doc-a", "a.pdf", 1, "Unique fragment on page 1"),
        _library_hit("doc-b", "b.pdf", 3, "Unique fragment on page 3"),
    ]
    *_, hits_by_ref = _build_library_prompt("q", hits)

    # Model uses legacy [d1 p1] and [d2 p3] where each page is unique
    citations, status = _library_citations_for("参考了 [d1 p1] 和 [d2 p3]", hits_by_ref)

    assert len(citations) == 2
    assert status == "verified"
    assert [c.document_id for c in citations] == ["doc-a", "doc-b"]


def test_library_citations_distinguishes_multiple_chunks_same_doc_and_page() -> None:
    hits = [
        _library_hit("doc-a", "a.pdf", 7, "Chunk 1 on page seven", rects=((10, 10, 50, 50),)),
        _library_hit("doc-a", "a.pdf", 7, "Chunk 2 on page seven", rects=((100, 100, 200, 200),)),
    ]
    *_, hits_by_ref = _build_library_prompt("q", hits)

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


def test_indexed_coverage_filters_missing_and_disabled_libraries(
    research_db: Database,
) -> None:
    _seed_document(
        research_db,
        content_id="9" * 64,
        display_name="paper.pdf",
        page_texts=[["Alpha beta gamma"]],
    )
    repository = RagChunkRepository(research_db)
    service = _service(research_db, FakeDeepSeek())

    # Initial ask populates index: 1 active document
    answer = service.ask_library(question="What is alpha?")
    assert answer.indexed_documents == 1
    assert repository.indexed_content_count() == 1
    assert repository.indexed_document_count() == 1
    assert len(repository.search_all('"alpha"', limit=10)) == 1

    # Mark document missing
    with research_db.transaction() as conn:
        conn.execute("UPDATE documents SET status = 'missing'")

    answer_missing = service.ask_library(question="What is alpha?")
    assert answer_missing.indexed_documents == 0
    assert repository.indexed_content_count() == 0
    assert repository.indexed_document_count() == 0
    assert len(repository.search_all('"alpha"', limit=10)) == 0

    # Restore document, but disable library
    with research_db.transaction() as conn:
        conn.execute("UPDATE documents SET status = 'valid_pdf'")
        conn.execute("UPDATE libraries SET status = 'disabled'")

    answer_disabled = service.ask_library(question="What is alpha?")
    assert answer_disabled.indexed_documents == 0
    assert repository.indexed_content_count() == 0
    assert repository.indexed_document_count() == 0
    assert len(repository.search_all('"alpha"', limit=10)) == 0


def test_indexed_coverage_multi_path_same_content(
    research_db: Database,
) -> None:
    # Two documents pointing to the SAME content_sha256
    shared_content = "a" * 64
    _seed_document(
        research_db,
        content_id=shared_content,
        display_name="doc1.pdf",
        page_texts=[["Machine learning and neural networks"]],
    )
    _seed_document(
        research_db,
        content_id=shared_content,
        display_name="doc2_copy.pdf",
        page_texts=[["Machine learning and neural networks"]],
    )
    repository = RagChunkRepository(research_db)
    service = _service(research_db, FakeDeepSeek())

    answer = service.ask_library(question="What is machine learning?")
    # 2 documents in library, 1 distinct content
    assert answer.indexed_documents == 2
    assert repository.indexed_document_count() == 2
    assert repository.indexed_content_count() == 1


def test_indexed_coverage_stale_parse_run_excluded(
    research_db: Database,
) -> None:
    content_id = "b" * 64
    _seed_document(
        research_db,
        content_id=content_id,
        display_name="stale_test.pdf",
        page_texts=[["Genomics and transcriptomics analysis"]],
    )
    repository = RagChunkRepository(research_db)
    service = _service(research_db, FakeDeepSeek())

    service.ask_library(question="What is genomics?")
    assert repository.indexed_document_count() == 1
    assert repository.indexed_content_count() == 1

    # Simulate new parse run completed for content, leaving old chunks in rag_chunks
    publish_pages(research_db, content_id, [make_page(1, ["Genomics update"])])

    # Before index rebuild, old chunks belong to outdated parse run -> excluded from active counts and search
    assert repository.indexed_document_count() == 0
    assert repository.indexed_content_count() == 0
    assert len(repository.search_all('"genomics"', limit=10)) == 0


def test_selected_scope_never_leaks_and_covers_each_matching_paper(research_db):
    first = _seed_document(research_db, content_id="a" * 64, display_name="chosen-a.pdf", page_texts=[["Alpha chosen first"]])
    second = _seed_document(research_db, content_id="b" * 64, display_name="chosen-b.pdf", page_texts=[["Alpha chosen second"]])
    _seed_document(research_db, content_id="c" * 64, display_name="excluded.pdf", page_texts=[["Alpha SECRET_OUTSIDE_SCOPE"]])
    deepseek = FakeDeepSeek(result="Compare [d1 r1 p1] and [d2 r1 p1]")
    service = _service(research_db, deepseek)
    service.ask_library(question="Alpha")  # Populate the whole-library index first.
    answer = service.ask_library(question="Alpha", document_ids=[first, second])
    assert {c.document_id for c in answer.citations} == {first, second}
    assert {c.content_id for c in answer.citations} == {"a" * 64, "b" * 64}
    assert "SECRET_OUTSIDE_SCOPE" not in deepseek.calls[-1]
    assert answer.indexed_documents == 2


def test_selected_chinese_overview_keeps_scope(research_db):
    first = _seed_document(research_db, content_id="d" * 64, display_name="chosen.pdf", page_texts=[["Abstract Alpha chosen evidence"]])
    _seed_document(research_db, content_id="e" * 64, display_name="excluded.pdf", page_texts=[["Abstract SECRET_OUTSIDE_SCOPE"]])
    deepseek = FakeDeepSeek(result_sequence=["OVERVIEW", "Evidence [d1 r1 p1]"])
    answer = _service(research_db, deepseek).ask_library(question="比较研究方法", document_ids=[first])
    assert [c.document_id for c in answer.citations] == [first]
    assert "SECRET_OUTSIDE_SCOPE" not in deepseek.calls[-1]


def test_missing_or_empty_selected_scope_never_falls_back_to_library(research_db):
    import pytest
    from literature_workbench.features.research.service import ResearchError
    _seed_document(research_db, content_id="f" * 64, display_name="excluded.pdf")
    deepseek = FakeDeepSeek()
    service = _service(research_db, deepseek)
    with pytest.raises(ResearchError, match="document_not_found"):
        service.ask_library(question="Alpha", document_ids=[str(uuid4())])
    with pytest.raises(ResearchError, match="invalid_document_scope"):
        service.ask_library(question="Alpha", document_ids=[])
    assert deepseek.calls == []


@pytest.mark.parametrize("local", [False, True])
def test_analysis_uses_real_scoped_retrieval_and_routes_both_model_modes(research_db, local):
    a = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf", page_texts=[["Sampling uses repeated visits to estimate detection."]])
    b = _seed_document(research_db, content_id="b" * 64, display_name="B.pdf", page_texts=[["Sampling uses a single visit and different detection assumptions."]])
    _seed_document(research_db, content_id="c" * 64, display_name="Excluded.pdf", page_texts=[["Sampling uses unrelated private data."]])
    response = '{"summary":"Insufficient comparable conditions","limitations":["Check sampling design"],"candidates":[]}'
    deepseek = FakeDeepSeek(result=response)
    qwen = FakeQwen(result=response)
    if local:
        settings = SettingsRepository(research_db)
        settings.update(default_translation_mode="local_only")
    answer = _service(research_db, deepseek, qwen=qwen).ask_library(question="Sampling", document_ids=[a, b], relation_analysis=True)
    assert {s.document_id for s in answer.source_fragments.values()} == {a, b}
    prompt = qwen.calls[0] if local else deepseek.calls[0]
    assert "Excluded.pdf" not in prompt and "unrelated private data" not in prompt
    assert "Return ONLY a JSON object" in (prompt if local else deepseek.system_prompts[0])
    assert parse_analysis(answer, [a, b])[2] == []
    if local:
        assert not deepseek.calls


def _full_text_result(quote="Alpha beta gamma", ref="[d1 p1]"):
    import json
    return json.dumps({"claims": [{"conclusion": "A bounded finding", "evidence": [{"ref": ref, "quote": quote}]}], "limitations": []})


def test_study_library_discovery_is_readonly_and_does_not_invent_uncited_full_coverage(research_db):
    a = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    b = _seed_document(research_db, content_id="b" * 64, display_name="B.pdf")
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(research_db, model)
    plan = service.prepare_full_text(question="Compare A and B", document_ids=[a, b])
    service.ask_full_text(question="Compare A and B", document_ids=[a, b], source_versions=plan['source_versions'])
    with research_db.connect() as c:
        before = [tuple(r) for r in c.execute('SELECT * FROM full_text_study_cache')]
    summary = {r['document_id']: r for r in service.study_summaries()['items']}
    assert summary[a]['status'] == 'studied' and summary[a]['reusable']
    assert summary[b]['status'] == 'history'
    history = service.study_history(b)
    assert history['total'] == 1 and history['api_call_required'] is False
    entry = history['items'][0]
    assert len(entry['documents']) == 2 and entry['current']
    assert entry['claims'][0]['evidence'][0]['document_id'] == a
    assert entry['claims'][0]['evidence'][0]['verified']
    assert len(model.calls) == 1
    with research_db.connect() as c:
        assert before == [tuple(r) for r in c.execute('SELECT * FROM full_text_study_cache')]


def test_study_library_changes_disable_old_evidence_even_if_quote_still_exists(research_db):
    a = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(research_db, model)
    plan = service.prepare_full_text(question="Explain", document_ids=[a])
    service.ask_full_text(question="Explain", document_ids=[a], source_versions=plan['source_versions'])
    with research_db.transaction() as c:
        c.execute("UPDATE pages SET normalized_text=normalized_text || ' New results.'")
    assert service.study_summaries()['items'][0]['status'] == 'stale'
    old = service.study_history(a)['items'][0]
    assert not old['current'] and not old['claims'][0]['evidence'][0]['verified']
    assert old['claims'][0]['evidence'][0]['content_id'] is None
    plan = service.prepare_full_text(question="Explain", document_ids=[a], force_refresh=True)
    service.ask_full_text(question="Explain", document_ids=[a], source_versions=plan['source_versions'], force_refresh=True)
    assert service.study_summaries()['items'][0]['status'] == 'studied'
    assert service.study_history(a)['total'] == 2 and len(model.calls) == 2


def test_study_library_legacy_records_model_change_and_corruption_are_explicit(research_db):
    a = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(research_db, model)
    plan = service.prepare_full_text(question="Explain", document_ids=[a])
    service.ask_full_text(question="Explain", document_ids=[a], source_versions=plan['source_versions'])
    with research_db.transaction() as c:
        c.execute('DELETE FROM full_text_study_documents')
    assert service.study_summaries()['items'][0]['status'] == 'studied'
    SettingsRepository(research_db).update(deepseek_model='another-model')
    assert not service.study_summaries()['items'][0]['reusable']
    with research_db.transaction() as c:
        c.execute('UPDATE full_text_study_cache SET output_json=?', (_full_text_result('Invented quotation'),))
    assert service.study_summaries()['items'][0]['status'] == 'history'
    invalid = service.study_history(a)['items'][0]
    assert invalid['invalid'] and not invalid['current'] and not invalid['claims']
    assert len(model.calls) == 1


def test_study_library_incremental_new_paper_has_full_witness_and_pagination(research_db):
    import json
    a = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    b = _seed_document(research_db, content_id="b" * 64, display_name="B.pdf")
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(research_db, model)
    plan = service.prepare_full_text(question="A", document_ids=[a])
    service.ask_full_text(question="A", document_ids=[a], source_versions=plan['source_versions'])
    model.result = json.dumps({'claims': [{'conclusion': 'Group finding', 'evidence': [
        {'ref': '[d1 p1]', 'quote': 'Alpha beta gamma'}, {'ref': '[d2 p1]', 'quote': 'Alpha beta gamma'}]}], 'limitations': []})
    plan = service.prepare_full_text(question="A+B", document_ids=[a, b])
    service.ask_full_text(question="A+B", document_ids=[a, b], source_versions=plan['source_versions'])
    assert all(item['status'] == 'studied' for item in service.study_summaries()['items'])
    b_entry = service.study_history(b)['items'][0]
    assert b_entry['context_kind'] == 'analysis_reuse' and b_entry['documents'][1]['read_mode'] == 'full_text'
    with research_db.transaction() as c:
        row = c.execute('SELECT * FROM full_text_study_cache ORDER BY created_at LIMIT 1').fetchone()
        for i in range(22):
            values = list(row)
            values[0] = f'copy-{i}'
            c.execute('INSERT INTO full_text_study_cache VALUES(?,?,?,?,?,?,?,?,?,?)', values)
    assert service.study_history(a)['has_more']
    assert len(service.study_history(a, offset=20)['items']) == 4
    assert len(model.calls) == 2


def test_full_text_supplies_every_page_without_search_and_verifies_quotes(research_db):
    import json
    doc1 = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf",
                          page_texts=[["Alpha beta gamma"], ["Unrelated appendix contains a crucial negative result."]])
    doc2 = _seed_document(research_db, content_id="b" * 64, display_name="B.pdf",
                          page_texts=[["Different conditions in paper B."], ["The final page must also be supplied."]])
    model = FakeDeepSeek(result=_full_text_result("The final page must also be supplied.", "[d2 p2]"))
    service = _service(research_db, model)
    prepared = service.prepare_full_text(question="Compare", document_ids=[doc1, doc2])
    assert model.calls == []
    assert [d["supplied_pages"] for d in prepared["coverage"]] == [2, 2]
    answer = service.ask_full_text(question="Compare", document_ids=[doc1, doc2], source_versions=prepared["source_versions"])
    assert len(model.calls) == 1 and model.purposes == ["research_full_text"]
    payload = json.loads(model.calls[0])
    assert len(payload["sources"]) == 2
    assert "crucial negative result" in payload["sources"][0]["pages"][1]["text"]
    assert payload["sources"][1]["pages"][1]["ref"] == "[d2 p2]"
    assert "untrusted" in model.system_prompts[0]
    assert answer.citations[0].document_id == doc2
    assert answer.citations[0].physical_page == 2
    assert answer.citations[0].excerpt == "The final page must also be supplied."
    assert answer.reading_mode == "full_text" and answer.reading_coverage == {**prepared, "cache_saved": True}
    assert "[d2 r2 p2]" in answer.answer
    assert RagChunkRepository(research_db).indexed_document_count() == 0


@pytest.mark.parametrize("problem,code", [("missing", "full_text_incomplete"), ("blank", "full_text_empty_page"), ("large", "full_text_too_large"), ("unavailable", "full_text_not_ready")])
def test_full_text_preflight_rejects_incomplete_unavailable_or_oversized_before_any_call(research_db, problem, code):
    from literature_workbench.features.research.full_text import FullTextError
    text = "x" * 270000 if problem == "large" else "Alpha beta gamma"
    doc = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf", page_texts=[[text]])
    with research_db.transaction() as c:
        if problem == "missing":
            c.execute("UPDATE contents SET page_count=2")
        if problem == "blank":
            c.execute("UPDATE pages SET normalized_text='   '")
        if problem == "unavailable":
            c.execute("UPDATE libraries SET status='removed'")
    model = FakeDeepSeek()
    with pytest.raises(FullTextError, match=code):
        _service(research_db, model).prepare_full_text(question="Compare", document_ids=[doc])
    assert not model.calls


@pytest.mark.parametrize("raw", [
    "Unsupported plain answer",
    '{"claims":[{"conclusion":"missing evidence","evidence":[]}],"limitations":[]}',
    _full_text_result("Invented quotation"),
    _full_text_result(ref="[d2 p1]"),
    _full_text_result(ref="[d1 p99]"),
    '{"claims":[],"limitations":[]}',
])
def test_full_text_never_returns_an_answer_with_unverified_evidence(research_db, raw):
    from literature_workbench.features.research.full_text import FullTextError
    doc = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    model = FakeDeepSeek(result=raw)
    service = _service(research_db, model)
    ready = service.prepare_full_text(question="Compare", document_ids=[doc])
    with pytest.raises(FullTextError, match="full_text_unverified_answer"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"])
    assert len(model.calls) == 1


def test_full_text_checks_versions_before_and_after_call_and_never_retries(research_db):
    from literature_workbench.features.research.full_text import FullTextError
    doc = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(research_db, model)
    ready = service.prepare_full_text(question="Compare", document_ids=[doc])
    with pytest.raises(FullTextError, match="full_text_source_changed"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions={doc: "wrong-version"})
    assert not model.calls

    class ChangingModel:
        calls = 0
        def answer_question(self, **kwargs):
            self.calls += 1
            with research_db.transaction() as c:
                c.execute("UPDATE documents SET status='missing'")
            return _full_text_result()
    changing = ChangingModel()
    service._deepseek = changing
    with pytest.raises(FullTextError, match="full_text_source_changed"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"])
    assert changing.calls == 1
    with research_db.transaction() as c:
        c.execute("UPDATE documents SET status='valid_pdf'")
    service._deepseek = FakeDeepSeek(error_sequence=["deepseek_total_timeout"])
    with pytest.raises(RuntimeError, match="deepseek_total_timeout"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"])
    assert len(service._deepseek.calls) == 1


def test_full_text_local_only_blocks_egress_and_allows_explicit_insufficient_evidence(research_db):
    from literature_workbench.features.research.full_text import FullTextError
    doc = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf")
    model = FakeDeepSeek(result='{"claims":[],"limitations":["无法从全文确认这个问题"]}')
    service = _service(research_db, model)
    ready = service.prepare_full_text(question="Compare", document_ids=[doc])
    service._settings.update(default_translation_mode="local_only")
    with pytest.raises(FullTextError, match="full_text_online_required"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"])
    assert not model.calls
    service._settings.update(default_translation_mode="online")
    answer = service.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"])
    assert answer.citation_status == "missing" and not answer.citations
    assert "未形成有原文依据的结论" in answer.answer


def _warm_study(database, *, question="Compare", long_text=False):
    first = _seed_document(database, content_id="a" * 64, display_name="A.pdf",
                           page_texts=[["Alpha beta gamma", *( ["Full paper background text and methods. " * 100] if long_text else [])]])
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(database, model)
    ready = service.prepare_full_text(question=question, document_ids=[first])
    answer = service.ask_full_text(question=question, document_ids=[first], source_versions=ready["source_versions"], plan_id=ready["plan_id"])
    return first, service, model, answer


def test_study_cache_survives_service_restart_and_returns_exact_answer_without_egress(research_db):
    doc, service, model, first = _warm_study(research_db)
    assert first.reading_coverage["api_call_required"] is True
    replacement = FakeDeepSeek(result="must not be called")
    restarted = _service(research_db, replacement)
    ready = restarted.prepare_full_text(question="Compare", document_ids=[doc])
    assert ready["cache_status"] == "hit" and ready["request_bytes"] == 0
    answer = restarted.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"], plan_id=ready["plan_id"])
    assert answer.answer == first.answer and answer.citations == first.citations
    assert answer.reading_coverage["api_call_required"] is False
    assert not replacement.calls and len(model.calls) == 1
    restarted._settings.update(default_translation_mode="local_only")
    assert restarted.prepare_full_text(question="Compare", document_ids=[doc])["cache_status"] == "hit"
    restarted.ask_full_text(question="Compare", document_ids=[doc], source_versions=ready["source_versions"])
    assert not replacement.calls


def test_study_cache_reordered_papers_reuse_quotes_but_need_their_own_answer(research_db):
    import json
    a = _seed_document(research_db, content_id="a" * 64, display_name="A.pdf", page_texts=[["Alpha paper original quotation."]])
    b = _seed_document(research_db, content_id="b" * 64, display_name="B.pdf", page_texts=[["Beta paper different original quotation."]])
    model = FakeDeepSeek(result=json.dumps({"claims":[{"conclusion":"Compare two sources", "evidence":[{"ref":"[d1 p1]","quote":"Alpha paper original quotation."},{"ref":"[d2 p1]","quote":"Beta paper different original quotation."}]}],"limitations":[]}))
    service = _service(research_db, model)
    p = service.prepare_full_text(question="Compare", document_ids=[a,b])
    service.ask_full_text(question="Compare", document_ids=[a,b], source_versions=p["source_versions"])
    p = service.prepare_full_text(question="Compare", document_ids=[b,a])
    assert p["cache_status"] == "miss" and p["context_kind"] == "analysis_reuse"
    model.result = json.dumps({"claims":[{"conclusion":"Compare two sources", "evidence":[{"ref":"[d2 p1]","quote":"Alpha paper original quotation."},{"ref":"[d1 p1]","quote":"Beta paper different original quotation."}]}],"limitations":[]})
    answer = service.ask_full_text(question="Compare", document_ids=[b,a], source_versions=p["source_versions"])
    assert len(model.calls) == 2
    assert [(c.document_id,c.excerpt) for c in answer.citations] == [(a,"Alpha paper original quotation."),(b,"Beta paper different original quotation.")]
    assert "[d2 r1 p1] [d1 r1 p1]" in answer.answer


def test_study_cache_uses_page_text_not_pdf_hash_or_parse_identity(research_db):
    doc, service, model, first = _warm_study(research_db)
    publish_pages(research_db, "b" * 64, [make_page(1, ["Alpha   beta\n gamma"])])
    with research_db.transaction() as c:
        c.execute("UPDATE documents SET content_sha256=? WHERE id=?", ("b"*64,doc))
    p = service.prepare_full_text(question="Compare", document_ids=[doc])
    assert p["cache_status"] == "hit"
    answer = service.ask_full_text(question="Compare", document_ids=[doc], source_versions=p["source_versions"])
    assert answer.citations[0].content_id == "b"*64 and len(model.calls) == 1
    publish_pages(research_db, "b" * 64, [make_page(1, ["Alpha beta gamma changed conclusion"])])
    p = service.prepare_full_text(question="Compare", document_ids=[doc])
    assert p["cache_status"] == "miss" and p["context_kind"] == "full_text"


def test_study_cache_new_question_uses_verified_notes_and_requires_a_paid_call(research_db):
    import json
    doc, service, model, first = _warm_study(research_db, long_text=True)
    p = service.prepare_full_text(question="Explain assumptions", document_ids=[doc])
    full = service.prepare_full_text(question="Explain assumptions", document_ids=[doc], reuse_analysis=False)
    assert p["context_kind"] == "analysis_reuse" and p["api_call_required"] is True
    assert p["request_bytes"] < full["request_bytes"]
    answer = service.ask_full_text(question="Explain assumptions", document_ids=[doc], source_versions=p["source_versions"], plan_id=p["plan_id"])
    assert len(model.calls) == 2 and "previous_studies" in json.loads(model.calls[-1])
    assert "have NOT been supplied all pages" in model.system_prompts[-1]
    assert answer.reading_coverage["context_kind"] == "analysis_reuse"
    assert service.prepare_full_text(question="Explain assumptions", document_ids=[doc])["cache_status"] == "hit"
    p = service.prepare_full_text(question="Compare", document_ids=[doc], force_refresh=True)
    assert p["cache_status"] == "miss" and p["context_kind"] == "full_text"
    service.ask_full_text(question="Compare", document_ids=[doc], source_versions=p["source_versions"], force_refresh=True, plan_id=p["plan_id"])
    assert len(model.calls) == 3


def test_reused_analysis_cannot_cite_unsupplied_original_even_if_it_exists_on_page(research_db):
    from literature_workbench.features.research.full_text import FullTextError
    doc, service, model, _ = _warm_study(research_db, long_text=True)
    p = service.prepare_full_text(question="Explain a new question", document_ids=[doc])
    model.result = _full_text_result("Full paper background text and methods.")
    with pytest.raises(FullTextError, match="full_text_unverified_answer"):
        service.ask_full_text(question="Explain a new question", document_ids=[doc], source_versions=p["source_versions"], plan_id=p["plan_id"])
    with research_db.connect() as c:
        assert c.execute("SELECT count(*) FROM full_text_study_cache").fetchone()[0] == 1


def test_changed_sending_plan_and_corrupt_cache_do_not_silently_charge(research_db):
    from literature_workbench.features.research.full_text import FullTextError
    doc, service, model, _ = _warm_study(research_db)
    p = service.prepare_full_text(question="Compare", document_ids=[doc])
    with research_db.transaction() as c:
        c.execute("UPDATE full_text_study_cache SET output_json='corrupt'")
    with pytest.raises(FullTextError, match="full_text_plan_changed"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions=p["source_versions"], plan_id=p["plan_id"])
    assert len(model.calls) == 1
    new_plan = service.prepare_full_text(question="Compare", document_ids=[doc])
    assert new_plan["api_call_required"] is True and new_plan["cache_status"] == "miss"


def test_model_and_prompt_changes_isolate_study_caches(research_db, monkeypatch):
    from literature_workbench.features.research import study_cache
    doc, service, model, _ = _warm_study(research_db)
    service._settings.update(deepseek_model="different-model")
    assert service.prepare_full_text(question="Compare", document_ids=[doc])["cache_status"] == "miss"
    service._settings.update(deepseek_model="deepseek-v4-flash")
    assert service.prepare_full_text(question="Compare", document_ids=[doc])["cache_status"] == "hit"
    monkeypatch.setattr(study_cache, "PROMPT_VERSION", "new-rules")
    assert service.prepare_full_text(question="Compare", document_ids=[doc])["cache_status"] == "miss"


def test_parallel_identical_studies_make_one_model_call(research_db):
    import concurrent.futures
    import time
    doc = _seed_document(research_db, content_id="a"*64, display_name="A.pdf")
    class SlowModel:
        calls = 0
        def answer_question(self, **kwargs):
            self.calls += 1
            time.sleep(.05)
            return _full_text_result()
    model = SlowModel()
    service = _service(research_db, model)
    p = service.prepare_full_text(question="Compare", document_ids=[doc])
    def ask():
        return service.ask_full_text(question="Compare", document_ids=[doc], source_versions=p["source_versions"], plan_id=p["plan_id"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: ask(), range(2)))
    assert model.calls == 1 and answers[0].answer == answers[1].answer
    assert sorted(a.reading_coverage["api_call_required"] for a in answers) == [False, True]


def test_study_cache_failures_are_explicit_without_automatic_paid_retries(research_db, monkeypatch):
    from literature_workbench.features.research.full_text import FullTextError
    from literature_workbench.features.research.study_cache import StudyCache
    doc = _seed_document(research_db, content_id="a"*64, display_name="A.pdf")
    model = FakeDeepSeek(result=_full_text_result())
    service = _service(research_db, model)
    p = service.prepare_full_text(question="Compare", document_ids=[doc])
    monkeypatch.setattr(StudyCache, "store", lambda *args: False)
    answer = service.ask_full_text(question="Compare", document_ids=[doc], source_versions=p["source_versions"])
    assert answer.reading_coverage["cache_saved"] is False and len(model.calls) == 1
    with research_db.transaction() as c:
        c.execute("DROP TABLE full_text_study_cache")
    with pytest.raises(FullTextError, match="full_text_cache_unavailable"):
        service.ask_full_text(question="Compare", document_ids=[doc], source_versions=p["source_versions"])
    assert len(model.calls) == 1


def _comparison_result(items, conclusion="A comparison with bounded evidence"):
    import json
    return json.dumps({"claims":[{"conclusion":conclusion,"evidence":[{"ref":ref,"quote":quote} for ref,quote in items]}],"limitations":[]})


def _seed_three_for_incremental(database):
    docs=[]
    for label,content in [("A","a"),("B","b"),("C","c")]:
        docs.append(_seed_document(database, content_id=content*64, display_name=label+'.pdf',
          page_texts=[[f"{label} verified original quotation.", f"{label}_FULL_TEXT_SENTINEL", "Methods background. "*100]]))
    return docs


def test_incremental_add_reads_only_new_paper_and_new_paper_is_reusable_next_time(research_db):
    import json
    a,b,c=_seed_three_for_incremental(research_db)
    model=FakeDeepSeek(result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B verified original quotation.")]))
    service=_service(research_db,model)
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"],plan_id=p["plan_id"])
    p=service.prepare_full_text(question="Compare",document_ids=[a,b,c])
    assert p["source_modes"]=={a:"analysis_reuse",b:"analysis_reuse",c:"full_text"}
    model.result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B verified original quotation."),("[d3 p1]","C verified original quotation.")],"ABC joint conclusion must not become C alone")
    answer=service.ask_full_text(question="Compare",document_ids=[a,b,c],source_versions=p["source_versions"],plan_id=p["plan_id"])
    sent=json.loads(model.calls[-1])
    assert len(model.calls)==2 and [d["filename"] for d in sent["new_documents"]]==["C.pdf"]
    assert "C_FULL_TEXT_SENTINEL" in model.calls[-1]
    assert "A_FULL_TEXT_SENTINEL" not in model.calls[-1] and "B_FULL_TEXT_SENTINEL" not in model.calls[-1]
    assert len(answer.citations)==3
    d=_seed_document(research_db,content_id="d"*64,display_name="D.pdf",page_texts=[["D verified original quotation."]])
    next_plan=service.prepare_full_text(question="Compare again",document_ids=[a,b,c,d])
    assert next_plan["source_modes"]=={a:"analysis_reuse",b:"analysis_reuse",c:"analysis_reuse",d:"full_text"}
    # Remove A/B from the selection: keep C's verified quote but not the joint claim.
    subset=service.prepare_full_text(question="Only C and D",document_ids=[c,d])
    assert subset["source_modes"]=={c:"analysis_reuse",d:"full_text"}
    model.result=_comparison_result([("[d1 p1]","C verified original quotation."),("[d2 p1]","D verified original quotation.")])
    service.ask_full_text(question="Only C and D",document_ids=[c,d],source_versions=subset["source_versions"],plan_id=subset["plan_id"])
    assert "ABC joint conclusion" not in model.calls[-1]
    assert "A verified" not in model.calls[-1] and "B verified" not in model.calls[-1]
    assert "A.pdf" not in model.calls[-1] and "B.pdf" not in model.calls[-1]
    assert len(model.calls)==3


def test_incremental_old_paper_can_cite_only_cached_quotes_not_unresent_full_text(research_db):
    from literature_workbench.features.research.full_text import FullTextError
    a,b,c=_seed_three_for_incremental(research_db)
    model=FakeDeepSeek(result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B verified original quotation.")]))
    service=_service(research_db,model)
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"])
    p=service.prepare_full_text(question="Compare",document_ids=[a,b,c])
    model.result=_comparison_result([("[d1 p1]","A_FULL_TEXT_SENTINEL")])
    with pytest.raises(FullTextError,match="full_text_unverified_answer"):
        service.ask_full_text(question="Compare",document_ids=[a,b,c],source_versions=p["source_versions"],plan_id=p["plan_id"])
    with research_db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM full_text_study_cache').fetchone()[0]==1
    assert len(model.calls)==2


def test_incremental_changed_paper_only_is_reread_and_cross_paper_claim_is_not_projected(research_db):
    import json
    a,b,c=_seed_three_for_incremental(research_db)
    model=FakeDeepSeek(result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B verified original quotation.")],"AB joint claim should not survive B changing"))
    service=_service(research_db,model)
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"])
    publish_pages(research_db,"b"*64,[make_page(1,["B changed original result."])])
    p=service.prepare_full_text(question="Changed comparison",document_ids=[a,b,c])
    assert p["source_modes"]=={a:"analysis_reuse",b:"full_text",c:"full_text"}
    model.result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B changed original result.")])
    service.ask_full_text(question="Changed comparison",document_ids=[a,b,c],source_versions=p["source_versions"],plan_id=p["plan_id"])
    sent=json.loads(model.calls[-1])
    assert len(sent["new_documents"])==2 and "AB joint claim" not in model.calls[-1]
    assert "A_FULL_TEXT_SENTINEL" not in model.calls[-1]


def test_incremental_never_counts_uncited_old_paper_as_available_analysis(research_db):
    a,b,c=_seed_three_for_incremental(research_db)
    model=FakeDeepSeek(result=_comparison_result([("[d1 p1]","A verified original quotation.")]))
    service=_service(research_db,model)
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"])
    p=service.prepare_full_text(question="Compare",document_ids=[a,b,c])
    assert p["source_modes"]=={a:"analysis_reuse",b:"full_text",c:"full_text"}
    assert len(model.calls)==1


def test_legacy_group_cache_is_reused_on_expansion_and_verified_witnesses_are_indexed(research_db):
    from literature_workbench.features.research.study_cache import LEGACY_PROMPT_VERSION, digest
    from literature_workbench.features.research.full_text import FullTextStudy
    from literature_workbench.features.research.repository import RagChunkRepository
    a,b,c=_seed_three_for_incremental(research_db)
    model=FakeDeepSeek(result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B verified original quotation.")]))
    service=_service(research_db,model)
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"])
    prepared=FullTextStudy(research_db,RagChunkRepository(research_db)).prepare([a,b],"Compare","deepseek-v4-flash")
    scope=digest([prepared.fingerprints,"deepseek-v4-flash",LEGACY_PROMPT_VERSION])
    key=digest([scope,"Compare"])
    with research_db.transaction() as connection:
        connection.execute("DELETE FROM full_text_study_documents")
        connection.execute("UPDATE full_text_study_cache SET scope_key=?,cache_key=?,prompt_version=?",(scope,key,LEGACY_PROMPT_VERSION))
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    cached=service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"])
    assert p["cache_status"]=="hit" and len(model.calls)==1 and len(cached.citations)==2
    p=service.prepare_full_text(question="Compare",document_ids=[a,b,c])
    assert p["source_modes"]=={a:"analysis_reuse",b:"analysis_reuse",c:"full_text"}
    model.result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","B verified original quotation."),("[d3 p1]","C verified original quotation.")])
    service.ask_full_text(question="Compare",document_ids=[a,b,c],source_versions=p["source_versions"],plan_id=p["plan_id"])
    with research_db.connect() as connection:
        members=connection.execute("SELECT document_id,read_mode FROM full_text_study_documents WHERE cache_key=?",(key,)).fetchall()
        assert {row[0]:row[1] for row in members}=={a:"full_text",b:"full_text"}
    assert len(model.calls)==2


def test_removed_original_group_member_drops_even_a_single_quoted_claim(research_db):
    a,b,c=_seed_three_for_incremental(research_db)
    model=FakeDeepSeek(result=_comparison_result([("[d1 p1]","A verified original quotation.")],"AB inference must not be attributed to A alone"))
    service=_service(research_db,model)
    p=service.prepare_full_text(question="Compare",document_ids=[a,b])
    service.ask_full_text(question="Compare",document_ids=[a,b],source_versions=p["source_versions"])
    p=service.prepare_full_text(question="Compare A C",document_ids=[a,c])
    assert p["source_modes"]=={a:"analysis_reuse",c:"full_text"}
    model.result=_comparison_result([("[d1 p1]","A verified original quotation."),("[d2 p1]","C verified original quotation.")])
    service.ask_full_text(question="Compare A C",document_ids=[a,c],source_versions=p["source_versions"],plan_id=p["plan_id"])
    assert "AB inference" not in model.calls[-1] and "B_FULL_TEXT_SENTINEL" not in model.calls[-1]
    assert "A verified original quotation." in model.calls[-1]


def test_incremental_does_not_silently_resend_old_full_text_for_tiny_papers(research_db):
    a,service,model,_=_warm_study(research_db)
    b=_seed_document(research_db,content_id="b"*64,display_name="B.pdf",page_texts=[["Tiny B original result."]])
    p=service.prepare_full_text(question="Compare with B",document_ids=[a,b])
    assert p["source_modes"]=={a:"analysis_reuse",b:"full_text"}
    assert p["context_kind"]=="analysis_reuse" and len(model.calls)==1
