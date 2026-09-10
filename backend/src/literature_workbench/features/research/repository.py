"""Persistence for research-assistant retrieval chunks."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.models import ParsedPage
from literature_workbench.features.parser.normalizer import _parsed_page_from_row
from literature_workbench.features.research.chunker import RagChunk


class RagChunkRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def read_pages(
        self, content_id: ContentId | str, parse_run_id: UUID | str
    ) -> list[ParsedPage]:
        """Read parsed pages explicitly bound to the given parse_run_id to avoid TOCTOU races."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,r.parser_name,r.parser_version
                FROM pages AS p
                JOIN parse_runs AS r ON r.id=p.parse_run_id AND r.status='completed'
                WHERE p.content_sha256=? AND p.parse_run_id=?
                ORDER BY p.page_index
                """,
                (str(content_id), str(parse_run_id)),
            ).fetchall()
        return [_parsed_page_from_row(row) for row in rows]

    def indexed_parse_run_id(self, content_id: ContentId | str) -> str | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT parse_run_id FROM rag_chunks WHERE content_sha256=? LIMIT 1",
                (str(content_id),),
            ).fetchone()
        return None if row is None else str(row["parse_run_id"])

    def rebuild(
        self,
        content_id: ContentId | str,
        parse_run_id: UUID | str,
        chunks: list[RagChunk],
    ) -> None:
        """Replace the chunk index for a document in one transaction."""
        timestamp = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM rag_chunks_fts
                WHERE chunk_id IN (SELECT id FROM rag_chunks WHERE content_sha256=?)
                """,
                (str(content_id),),
            )
            connection.execute(
                "DELETE FROM rag_chunks WHERE content_sha256=?",
                (str(content_id),),
            )
            for chunk in chunks:
                chunk_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO rag_chunks(
                      id,content_sha256,parse_run_id,physical_page,chunk_index,
                      text,char_count,rects_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        chunk_id,
                        str(content_id),
                        str(parse_run_id),
                        chunk.physical_page,
                        chunk.chunk_index,
                        chunk.text,
                        len(chunk.text),
                        json.dumps([list(rect) for rect in chunk.rects]),
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO rag_chunks_fts(chunk_id,physical_page,text)
                    VALUES(?,?,?)
                    """,
                    (chunk_id, chunk.physical_page, chunk.text),
                )

    def overview_chunks(
        self,
        content_id: ContentId | str,
        *,
        limit: int,
    ) -> list[sqlite3.Row]:
        """Leading chunks in reading order — the title/abstract region used
        to answer document-overview questions."""
        with self._database.connect() as connection:
            return connection.execute(
                """
                SELECT id AS chunk_id,physical_page,text,rects_json
                FROM rag_chunks
                WHERE content_sha256=?
                ORDER BY physical_page,chunk_index
                LIMIT ?
                """,
                (str(content_id), limit),
            ).fetchall()

    def parsed_documents(self) -> list[tuple[str, str, str]]:
        """(document_id, content_sha256, current_parse_run_id) for every
        active document whose parse is complete — the sweep's index candidates."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.id,d.content_sha256,c.current_parse_run_id
                FROM documents AS d
                JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
                JOIN contents AS c ON c.sha256=d.content_sha256
                WHERE d.status IN ('candidate_pdf','valid_pdf') AND d.format='pdf'
                  AND c.current_parse_run_id IS NOT NULL
                """
            ).fetchall()
        return [
            (str(row["id"]), str(row["content_sha256"]), str(row["current_parse_run_id"]))
            for row in rows
        ]

    def indexed_content_count(self) -> int:
        """Distinct active, valid contents with a chunk index matching current parse run."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT count(DISTINCT d.content_sha256)
                FROM rag_chunks AS c
                LEFT JOIN contents AS co ON co.sha256 = c.content_sha256
                JOIN documents AS d ON d.content_sha256 = c.content_sha256
                JOIN libraries AS l ON l.id = d.library_id AND l.status = 'active'
                WHERE d.status IN ('candidate_pdf', 'valid_pdf') AND d.format = 'pdf'
                  AND (co.current_parse_run_id IS NULL OR co.current_parse_run_id = c.parse_run_id)
                """
            ).fetchone()
        return int(row[0])

    def indexed_document_count(self) -> int:
        """Distinct active, valid documents with a chunk index matching current parse run."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT count(DISTINCT d.id)
                FROM rag_chunks AS c
                LEFT JOIN contents AS co ON co.sha256 = c.content_sha256
                JOIN documents AS d ON d.content_sha256 = c.content_sha256
                JOIN libraries AS l ON l.id = d.library_id AND l.status = 'active'
                WHERE d.status IN ('candidate_pdf', 'valid_pdf') AND d.format = 'pdf'
                  AND (co.current_parse_run_id IS NULL OR co.current_parse_run_id = c.parse_run_id)
                """
            ).fetchone()
        return int(row[0])

    def search_all(
        self,
        match_query: str,
        *,
        limit: int,
    ) -> list[sqlite3.Row]:
        """Corpus-wide FTS search with document attribution.

        Rows come back score-ordered and may contain several rows per
        document and per content; the retriever applies dedupe and the
        per-document cap as policy.
        """
        with self._database.connect() as connection:
            return connection.execute(
                """
                SELECT c.id AS chunk_id,c.content_sha256,c.physical_page,c.text,
                       c.rects_json,d.id AS document_id,d.display_name,
                       bm25(rag_chunks_fts) AS score
                FROM rag_chunks_fts
                JOIN rag_chunks AS c ON c.id=rag_chunks_fts.chunk_id
                LEFT JOIN contents AS co ON co.sha256=c.content_sha256
                JOIN documents AS d ON d.content_sha256=c.content_sha256
                  AND d.status IN ('candidate_pdf','valid_pdf') AND d.format='pdf'
                JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
                WHERE rag_chunks_fts MATCH ?
                  AND (co.current_parse_run_id IS NULL OR co.current_parse_run_id = c.parse_run_id)
                ORDER BY score
                LIMIT ?
                """,
                (match_query, limit),
            ).fetchall()

    def search(
        self,
        content_id: ContentId | str,
        match_query: str,
        *,
        limit: int,
    ) -> list[sqlite3.Row]:
        with self._database.connect() as connection:
            return connection.execute(
                """
                SELECT c.id AS chunk_id,c.physical_page,c.text,c.rects_json,
                       bm25(rag_chunks_fts) AS score
                FROM rag_chunks_fts
                JOIN rag_chunks AS c ON c.id=rag_chunks_fts.chunk_id
                WHERE rag_chunks_fts MATCH ? AND c.content_sha256=?
                ORDER BY score
                LIMIT ?
                """,
                (match_query, str(content_id), limit),
            ).fetchall()
