"""FTS5 lexical retrieval over a document's chunk index.

The index uses the trigram tokenizer, which matches 3+ character
substrings: Latin terms behave like prefix-insensitive contains-queries,
and CJK text — which unicode61 cannot segment — becomes searchable
directly.  Question analysis therefore extracts both Latin tokens and
short CJK windows.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.research.chunker import BBox
from literature_workbench.features.research.repository import RagChunkRepository

DEFAULT_HIT_LIMIT = 6
DEFAULT_LIBRARY_HIT_LIMIT = 8
DEFAULT_PER_DOCUMENT_LIMIT = 3
_LIBRARY_FETCH_MULTIPLIER = 5
_MAX_QUERY_TOKENS = 16
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-']*")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿]+")
# CJK windows longer than this are sliced so a long question run still
# produces matchable substrings; trigram needs at least 3 characters.
_CJK_WINDOW = 4
_CJK_STEP = 2


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk_id: str
    physical_page: int
    text: str
    score: float
    rects: tuple[BBox, ...] = ()


@dataclass(frozen=True, slots=True)
class LibraryRetrievalHit:
    chunk_id: str
    document_id: str
    display_name: str
    physical_page: int
    text: str
    score: float
    rects: tuple[BBox, ...] = ()


def _cjk_windows(run: str) -> list[str]:
    if len(run) <= _CJK_WINDOW:
        return [run] if len(run) >= 3 else []
    windows = [
        run[start : start + _CJK_WINDOW]
        for start in range(0, len(run) - _CJK_WINDOW + 1, _CJK_STEP)
    ]
    # Always include the tail window so the run's end stays matchable.
    tail = run[len(run) - _CJK_WINDOW :]
    if windows[-1] != tail:
        windows.append(tail)
    return windows


def build_match_query(question: str) -> str | None:
    """Build an FTS5 MATCH expression from a free-form question.

    Latin tokens (3+ characters, the trigram minimum) carry the signal for
    English papers; CJK runs are sliced into overlapping 4-character
    windows so Chinese questions match Chinese corpora directly.  Every
    token is double-quoted so user punctuation cannot break the MATCH
    syntax.  Returns ``None`` when the question has no usable token.
    """
    tokens: list[str] = []
    for match in _LATIN_TOKEN.finditer(question):
        token = match.group(0)
        if len(token) < 3 or token in tokens:
            continue
        tokens.append(token)
        if len(tokens) >= _MAX_QUERY_TOKENS:
            break
    for match in _CJK_RUN.finditer(question):
        for window in _cjk_windows(match.group(0)):
            if window in tokens:
                continue
            tokens.append(window)
            if len(tokens) >= _MAX_QUERY_TOKENS:
                break
        if len(tokens) >= _MAX_QUERY_TOKENS:
            break
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def rects_from_json(raw: str | None) -> tuple[BBox, ...]:
    if not raw or not isinstance(raw, str):
        return ()
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(value, list):
        return ()
    rects: list[BBox] = []
    for item in value:
        if not (isinstance(item, (list, tuple)) and len(item) == 4):
            continue
        try:
            x0_f = float(item[0])
            y0_f = float(item[1])
            x1_f = float(item[2])
            y1_f = float(item[3])
            if (
                math.isnan(x0_f)
                or math.isnan(y0_f)
                or math.isnan(x1_f)
                or math.isnan(y1_f)
                or math.isinf(x0_f)
                or math.isinf(y0_f)
                or math.isinf(x1_f)
                or math.isinf(y1_f)
            ):
                continue
            x0 = int(round(x0_f))
            y0 = int(round(y0_f))
            x1 = int(round(x1_f))
            y1 = int(round(y1_f))
        except (ValueError, TypeError, OverflowError):
            continue
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
            continue
        rects.append((x0, y0, x1, y1))
    return tuple(rects)


class RagRetriever:
    def __init__(self, repository: RagChunkRepository) -> None:
        self._repository = repository

    def search(
        self,
        content_id: ContentId | str,
        question: str,
        *,
        limit: int = DEFAULT_HIT_LIMIT,
    ) -> list[RetrievalHit]:
        match_query = build_match_query(question)
        if match_query is None:
            return []
        return self.search_match_query(content_id, match_query, limit=limit)

    def search_match_query(
        self,
        content_id: ContentId | str,
        match_query: str,
        *,
        limit: int = DEFAULT_HIT_LIMIT,
    ) -> list[RetrievalHit]:
        rows = self._repository.search(content_id, match_query, limit=limit)
        return [
            RetrievalHit(
                chunk_id=str(row["chunk_id"]),
                physical_page=int(row["physical_page"]),
                text=str(row["text"]),
                score=float(row["score"]),
                rects=rects_from_json(row["rects_json"]),
            )
            for row in rows
        ]

    def overview(
        self,
        content_id: ContentId | str,
        *,
        limit: int = DEFAULT_HIT_LIMIT,
    ) -> list[RetrievalHit]:
        rows = self._repository.overview_chunks(content_id, limit=limit)
        return [
            RetrievalHit(
                chunk_id=str(row["chunk_id"]),
                physical_page=int(row["physical_page"]),
                text=str(row["text"]),
                score=0.0,
                rects=rects_from_json(row["rects_json"]),
            )
            for row in rows
        ]

    def search_library(
        self,
        question: str,
        *,
        limit: int = DEFAULT_LIBRARY_HIT_LIMIT,
        per_document: int = DEFAULT_PER_DOCUMENT_LIMIT,
    ) -> list[LibraryRetrievalHit]:
        """Corpus-wide search: dedupe by chunk across hard links, cap
        fragments per document so one long paper cannot crowd out the rest."""
        match_query = build_match_query(question)
        if match_query is None:
            return []
        return self.search_library_match_query(
            match_query, limit=limit, per_document=per_document
        )

    def search_library_match_query(
        self,
        match_query: str,
        *,
        limit: int = DEFAULT_LIBRARY_HIT_LIMIT,
        per_document: int = DEFAULT_PER_DOCUMENT_LIMIT,
    ) -> list[LibraryRetrievalHit]:
        rows = self._repository.search_all(
            match_query, limit=max(limit * _LIBRARY_FETCH_MULTIPLIER, 20)
        )
        hits: list[LibraryRetrievalHit] = []
        seen_chunks: set[str] = set()
        per_document_count: dict[str, int] = {}
        for row in rows:
            # The JOIN multiplies a chunk by every document hard-linking its
            # content; keep any single registration per chunk.
            chunk_id = str(row["chunk_id"])
            if chunk_id in seen_chunks:
                continue
            document_id = str(row["document_id"])
            if per_document_count.get(document_id, 0) >= per_document:
                continue
            seen_chunks.add(chunk_id)
            per_document_count[document_id] = per_document_count.get(document_id, 0) + 1
            hits.append(
                LibraryRetrievalHit(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    display_name=str(row["display_name"]),
                    physical_page=int(row["physical_page"]),
                    text=str(row["text"]),
                    score=float(row["score"]),
                    rects=rects_from_json(row["rects_json"]),
                )
            )
            if len(hits) >= limit:
                break
        return hits
