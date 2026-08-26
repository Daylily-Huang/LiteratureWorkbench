"""Single-document research Q&A orchestration.

Synchronous by design for the MVP: one DeepSeek round-trip (~seconds)
needs no task queue.  Retry and local-fallback semantics deliberately
mirror ``translation/service.py`` so both features behave identically
from the user's point of view.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.library.models import ContentId
from literature_workbench.features.models.qwen_local import QwenLocalError
from literature_workbench.features.models.qwen_process import QwenProcessError
from literature_workbench.features.research.chunker import chunk_pages
from literature_workbench.features.research.expansion import (
    QueryExpansion,
    build_expansion_prompt,
    contains_cjk,
    parse_expansion_response,
)
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.retriever import (
    LibraryRetrievalHit,
    RagRetriever,
    RetrievalHit,
)
from literature_workbench.features.settings.repository import SettingsRepository

_FALLBACK_ERRORS = {
    "deepseek_http_error",
    "deepseek_transport_failed",
    "deepseek_total_timeout",
    "deepseek_key_unavailable",
    "deepseek_authorization_failed",
    "invalid_deepseek_response",
}

_RETRYABLE_ERRORS = {
    "deepseek_http_error",
    "deepseek_transport_failed",
    "deepseek_total_timeout",
}

_CITATION_MARK = re.compile(r"\[p(\d+)\]")
_LIBRARY_CITATION_MARK = re.compile(r"\[d(\d+)\s+p(\d+)\]")
_NO_HIT_MESSAGE = (
    "在当前文档中没有检索到与问题相关的内容片段，请换个问法或确认文档已完成解析。"
)
_NO_HIT_LIBRARY_MESSAGE = (
    "在当前文献库中没有检索到与问题相关的内容片段，请换个问法或确认文献已完成解析。"
)
_OVERVIEW_FALLBACK_QUERY = '"摘要" OR "abstract" OR "Abstract" OR "结论" OR "summary"'

_MAX_QUESTION_CHARS = 2000


class ResearchError(RuntimeError):
    pass


class ResearchDeepSeekClient(Protocol):
    def answer_question(
        self,
        *,
        task_id: str,
        document_id: UUID | str | None,
        content_sha256: str | None,
        display_file_name: str | None,
        model: str,
        prompt: str,
        purpose: str = "research_question",
    ) -> str: ...


class ResearchQwenClient(Protocol):
    def translate(self, prompt: str) -> str: ...


class QwenProcess(Protocol):
    def ensure_running(self) -> object: ...


@dataclass(frozen=True, slots=True)
class ResearchCitation:
    physical_page: int
    excerpt: str
    rects: tuple[tuple[int, int, int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchAnswer:
    answer: str
    citations: list[ResearchCitation]
    provider: str
    model: str
    fallback_reason: str | None = field(default=None)


@dataclass(frozen=True, slots=True)
class LibraryCitation:
    document_id: str
    display_name: str
    physical_page: int
    excerpt: str
    rects: tuple[tuple[int, int, int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class LibraryAnswer:
    answer: str
    citations: list[LibraryCitation]
    provider: str
    model: str
    fallback_reason: str | None = field(default=None)
    indexed_documents: int = 0


class ResearchService:
    def __init__(
        self,
        database: Database,
        repository: RagChunkRepository,
        retriever: RagRetriever,
        settings: SettingsRepository,
        deepseek: ResearchDeepSeekClient,
        qwen: ResearchQwenClient,
        qwen_process: QwenProcess,
    ) -> None:
        self._database = database
        self._repository = repository
        self._retriever = retriever
        self._settings = settings
        self._deepseek = deepseek
        self._qwen = qwen
        self._qwen_process = qwen_process

    def ask(self, *, document_id: UUID | str, question: str) -> ResearchAnswer:
        question = question.strip()
        if not question:
            raise ResearchError("question_empty")
        if len(question) > _MAX_QUESTION_CHARS:
            raise ResearchError("question_too_long")
        document = self._read_document(document_id)
        if document is None:
            raise ResearchError("document_not_found")
        content_id, parse_run_id, display_name = document
        if parse_run_id is None:
            raise ResearchError("parse_required")
        self._ensure_index(content_id, parse_run_id)
        settings = self._settings.read()
        hits = self._retriever.search(content_id, question)
        if (
            not hits
            and contains_cjk(question)
            and settings.default_translation_mode != "local_only"
        ):
            # A Chinese question has no Latin tokens for the FTS index of an
            # English paper; translate/expand it once via DeepSeek.  In
            # local_only mode nothing may leave the machine, so the canned
            # answer is kept instead.
            hits = self._expand_and_search(
                document_id=document_id,
                content_sha256=str(content_id),
                display_name=display_name,
                model=settings.deepseek_model,
                question=question,
            )
        if not hits:
            return ResearchAnswer(
                answer=_NO_HIT_MESSAGE,
                citations=[],
                provider="retrieval_only",
                model="",
            )
        prompt = _build_prompt(question, hits)
        if settings.default_translation_mode == "local_only":
            return self._ask_qwen(prompt, hits, fallback_reason=None)
        try:
            answer = self._ask_deepseek_with_one_retry(
                document_id=document_id,
                content_sha256=str(content_id),
                display_name=display_name,
                model=settings.deepseek_model,
                prompt=prompt,
            )
        except EgressRejected as error:
            code = str(error)
            if code in _FALLBACK_ERRORS and settings.qwen_fallback_enabled:
                return self._ask_qwen(prompt, hits, fallback_reason=code)
            raise ResearchError(code or "deepseek_failed") from None
        return ResearchAnswer(
            answer=answer,
            citations=_citations_for(answer, hits),
            provider="deepseek",
            model=settings.deepseek_model,
        )

    def ask_library(self, *, question: str) -> LibraryAnswer:
        question = question.strip()
        if not question:
            raise ResearchError("question_empty")
        if len(question) > _MAX_QUESTION_CHARS:
            raise ResearchError("question_too_long")
        self._ensure_library_index()
        indexed_documents = self._repository.indexed_content_count()
        settings = self._settings.read()
        hits = self._retriever.search_library(question)
        if (
            not hits
            and contains_cjk(question)
            and settings.default_translation_mode != "local_only"
        ):
            expansion = self._expand_library(
                model=settings.deepseek_model, question=question
            )
            if expansion is not None:
                if expansion.kind == "overview":
                    # "总览文献库"没有单一语料可喂；退化为摘要类关键词检索。
                    hits = self._retriever.search_library_match_query(
                        _OVERVIEW_FALLBACK_QUERY
                    )
                else:
                    assert expansion.match_query is not None
                    hits = self._retriever.search_library_match_query(
                        expansion.match_query
                    )
        if not hits:
            return LibraryAnswer(
                answer=_NO_HIT_LIBRARY_MESSAGE,
                citations=[],
                provider="retrieval_only",
                model="",
                indexed_documents=indexed_documents,
            )
        prompt, hits_by_ref = _build_library_prompt(question, hits)
        if settings.default_translation_mode == "local_only":
            return self._ask_library_qwen(
                prompt, hits_by_ref, indexed_documents, fallback_reason=None
            )
        try:
            answer = self._ask_deepseek_with_one_retry(
                document_id=None,
                content_sha256=None,
                display_name=None,
                model=settings.deepseek_model,
                prompt=prompt,
            )
        except EgressRejected as error:
            code = str(error)
            if code in _FALLBACK_ERRORS and settings.qwen_fallback_enabled:
                return self._ask_library_qwen(
                    prompt, hits_by_ref, indexed_documents, fallback_reason=code
                )
            raise ResearchError(code or "deepseek_failed") from None
        return LibraryAnswer(
            answer=answer,
            citations=_library_citations_for(answer, hits_by_ref),
            provider="deepseek",
            model=settings.deepseek_model,
            indexed_documents=indexed_documents,
        )

    def _ensure_library_index(self) -> None:
        """Incrementally index every parsed document whose chunks are stale
        or missing; per-document failures are skipped, not fatal."""
        seen_contents: set[str] = set()
        for _document_id, content_sha256, parse_run_id in self._repository.parsed_documents():
            if content_sha256 in seen_contents:
                continue
            seen_contents.add(content_sha256)
            if self._repository.indexed_parse_run_id(content_sha256) == parse_run_id:
                continue
            try:
                pages = self._repository.read_current_pages(content_sha256)
                if not pages:
                    continue
                self._repository.rebuild(
                    content_sha256, parse_run_id, chunk_pages(pages)
                )
            except (sqlite3.Error, ValueError):
                continue

    def _expand_library(self, *, model: str, question: str) -> QueryExpansion | None:
        try:
            response = self._deepseek.answer_question(
                task_id=f"research-expand:{uuid4()}",
                document_id=None,
                content_sha256=None,
                display_file_name=None,
                model=model,
                prompt=build_expansion_prompt(question),
                purpose="research_query_expansion",
            )
        except EgressRejected as error:
            raise ResearchError(str(error) or "deepseek_failed") from None
        return parse_expansion_response(response)

    def _ask_library_qwen(
        self,
        prompt: str,
        hits_by_ref: dict[tuple[int, int], LibraryRetrievalHit],
        indexed_documents: int,
        fallback_reason: str | None,
    ) -> LibraryAnswer:
        try:
            self._qwen_process.ensure_running()
            answer = self._qwen.translate(prompt)
        except (QwenProcessError, QwenLocalError) as error:
            raise ResearchError(str(error) or "qwen_failed") from None
        except (OSError, RuntimeError):
            raise ResearchError("qwen_failed") from None
        return LibraryAnswer(
            answer=answer,
            citations=_library_citations_for(answer, hits_by_ref),
            provider="qwen",
            model="qwen-local",
            fallback_reason=fallback_reason,
            indexed_documents=indexed_documents,
        )

    def _read_document(
        self, document_id: UUID | str
    ) -> tuple[ContentId, str | None, str | None] | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.content_sha256,d.display_name,c.current_parse_run_id
                FROM documents AS d
                LEFT JOIN contents AS c ON c.sha256=d.content_sha256
                WHERE d.id=? AND d.format='pdf'
                """,
                (str(document_id),),
            ).fetchone()
        if row is None or row["content_sha256"] is None:
            return None
        parse_run_id = row["current_parse_run_id"]
        return (
            ContentId(str(row["content_sha256"])),
            None if parse_run_id is None else str(parse_run_id),
            None if row["display_name"] is None else str(row["display_name"]),
        )

    def _ensure_index(self, content_id: ContentId, parse_run_id: str) -> None:
        if self._repository.indexed_parse_run_id(content_id) == parse_run_id:
            return
        pages = self._repository.read_current_pages(content_id)
        if not pages:
            raise ResearchError("parse_required")
        self._repository.rebuild(content_id, parse_run_id, chunk_pages(pages))

    def _expand_and_search(
        self,
        *,
        document_id: UUID | str,
        content_sha256: str,
        display_name: str | None,
        model: str,
        question: str,
    ) -> list[RetrievalHit]:
        try:
            response = self._deepseek.answer_question(
                task_id=f"research-expand:{uuid4()}",
                document_id=document_id,
                content_sha256=content_sha256,
                display_file_name=display_name,
                model=model,
                prompt=build_expansion_prompt(question),
                purpose="research_query_expansion",
            )
        except EgressRejected as error:
            # Surface the real setup/connectivity code rather than a
            # misleading "no relevant content" message.
            raise ResearchError(str(error) or "deepseek_failed") from None
        expansion = parse_expansion_response(response)
        if expansion is None:
            return []
        if expansion.kind == "overview":
            return self._retriever.overview(content_sha256)
        assert expansion.match_query is not None
        return self._retriever.search_match_query(content_sha256, expansion.match_query)

    def _ask_deepseek_with_one_retry(
        self,
        *,
        document_id: UUID | str | None,
        content_sha256: str | None,
        display_name: str | None,
        model: str,
        prompt: str,
    ) -> str:
        task_id = f"research:{uuid4()}"
        try:
            return self._deepseek.answer_question(
                task_id=task_id,
                document_id=document_id,
                content_sha256=content_sha256,
                display_file_name=display_name,
                model=model,
                prompt=prompt,
            )
        except EgressRejected as first_error:
            if str(first_error) not in _RETRYABLE_ERRORS:
                raise
        # Every attempt is audited separately (attempt_no), so the retry
        # leaves a complete trail before any fallback decision.
        return self._deepseek.answer_question(
            task_id=task_id,
            document_id=document_id,
            content_sha256=content_sha256,
            display_file_name=display_name,
            model=model,
            prompt=prompt,
        )

    def _ask_qwen(
        self,
        prompt: str,
        hits: list[RetrievalHit],
        fallback_reason: str | None,
    ) -> ResearchAnswer:
        try:
            self._qwen_process.ensure_running()
            answer = self._qwen.translate(prompt)
        except (QwenProcessError, QwenLocalError) as error:
            raise ResearchError(str(error) or "qwen_failed") from None
        except (OSError, RuntimeError):
            raise ResearchError("qwen_failed") from None
        return ResearchAnswer(
            answer=answer,
            citations=_citations_for(answer, hits),
            provider="qwen",
            model="qwen-local",
            fallback_reason=fallback_reason,
        )


def _build_prompt(question: str, hits: list[RetrievalHit]) -> str:
    fragments = "\n\n".join(
        f"[p{hit.physical_page}] {hit.text}" for hit in hits
    )
    return (
        "You are a research assistant helping a reader understand one PDF "
        "document. Answer the question using ONLY the fragments below, each "
        "prefixed with its physical page as [pN]. Cite the supporting page "
        "immediately after every claim using the same [pN] marks. If the "
        "fragments do not contain the answer, say so plainly instead of "
        "guessing. Answer in the question's language.\n\n"
        f"<fragments>\n{fragments}\n</fragments>\n\n"
        f"<question>\n{question}\n</question>"
    )


def _citations_for(answer: str, hits: list[RetrievalHit]) -> list[ResearchCitation]:
    pages_by_hit = {hit.physical_page: hit for hit in hits}
    cited: list[int] = []
    for mark in _CITATION_MARK.finditer(answer):
        page = int(mark.group(1))
        if page in pages_by_hit and page not in cited:
            cited.append(page)
    if not cited:
        cited = [hit.physical_page for hit in hits[:2]]
    return [
        ResearchCitation(
            physical_page=page,
            excerpt=_excerpt(pages_by_hit[page].text),
            rects=pages_by_hit[page].rects,
        )
        for page in cited
    ]


def _excerpt(text: str, *, limit: int = 160) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _build_library_prompt(
    question: str, hits: list[LibraryRetrievalHit]
) -> tuple[str, dict[tuple[int, int], LibraryRetrievalHit]]:
    """Build the cross-document prompt and the (doc-number, page) lookup.

    Documents are numbered d1..dN in first-appearance order; fragments are
    tagged ``[dN pM]`` so the model can cite both the document and the page.
    """
    doc_numbers: dict[str, int] = {}
    fragments: list[str] = []
    hits_by_ref: dict[tuple[int, int], LibraryRetrievalHit] = {}
    for hit in hits:
        if hit.document_id not in doc_numbers:
            doc_numbers[hit.document_id] = len(doc_numbers) + 1
        doc_number = doc_numbers[hit.document_id]
        fragments.append(f"[d{doc_number} p{hit.physical_page}] {hit.text}")
        hits_by_ref[(doc_number, hit.physical_page)] = hit
    seen: set[int] = set()
    document_lines: list[str] = []
    for hit in hits:
        number = doc_numbers[hit.document_id]
        if number in seen:
            continue
        seen.add(number)
        document_lines.append(f"d{number}: 《{hit.display_name}》")
    documents = "\n".join(document_lines)
    prompt = (
        "You are a research assistant answering questions across a personal "
        "PDF library. Answer the question using ONLY the fragments below, "
        "each prefixed with its source as [dN pM] where dN is the document "
        "number listed under <documents> and pM is its physical page. Cite "
        "the supporting source immediately after every claim using the same "
        "[dN pM] marks, and use the relevant document whenever several "
        "fragments contribute. If the fragments do not contain the answer, "
        "say so plainly instead of guessing. Answer in the question's "
        "language.\n\n"
        f"<documents>\n{documents}\n</documents>\n\n"
        "<fragments>\n" + "\n\n".join(fragments) + "\n</fragments>\n\n"
        f"<question>\n{question}\n</question>"
    )
    return prompt, hits_by_ref


def _library_citations_for(
    answer: str, hits_by_ref: dict[tuple[int, int], LibraryRetrievalHit]
) -> list[LibraryCitation]:
    cited: list[tuple[int, int]] = []
    for mark in _LIBRARY_CITATION_MARK.finditer(answer):
        ref = (int(mark.group(1)), int(mark.group(2)))
        if ref in hits_by_ref and ref not in cited:
            cited.append(ref)
    if not cited:
        # The model ignored the marking scheme; fall back to the strongest
        # fragments so the user still gets a trail into the library.
        cited = list(hits_by_ref)[:2]
    return [
        LibraryCitation(
            document_id=hits_by_ref[ref].document_id,
            display_name=hits_by_ref[ref].display_name,
            physical_page=ref[1],
            excerpt=_excerpt(hits_by_ref[ref].text),
            rects=hits_by_ref[ref].rects,
        )
        for ref in cited
    ]
