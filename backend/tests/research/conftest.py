from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.parser.models import ParsedBlock, ParsedPage


def make_block(text: str) -> ParsedBlock:
    return ParsedBlock(
        block_id=f"0:{hash(text) % 1000}",
        kind="text",
        text=text,
        bbox_1000=(10, 20, 300, 80),
        source="pymupdf",
        confidence=None,
    )


def make_page(physical_page: int, block_texts: list[str]) -> ParsedPage:
    return ParsedPage(
        content_id="c" * 64,  # type: ignore[arg-type]
        page_index=physical_page - 1,
        physical_page=physical_page,
        parse_run_id=uuid4(),
        parser_name="pymupdf",
        parser_version="1.0",
        normalized_text=" ".join(block_texts),
        raw_text="\n".join(block_texts),
        width_points=595,
        height_points=790,
        coordinate_reliable=True,
        blocks=[make_block(text) for text in block_texts],
    )


@pytest.fixture
def research_db(tmp_path: Path) -> Database:
    database = Database.open(tmp_path / "db" / "research.sqlite3")
    run_migrations(database)
    return database


def publish_pages(database: Database, content_id: str, pages: list[ParsedPage]) -> str:
    """Insert a completed parse run with its pages and point the content at it."""
    run_id = str(uuid4())
    timestamp = datetime.now(UTC).isoformat()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO contents(
              sha256,size_bytes,mime_type,page_count,status,created_at,updated_at)
            VALUES(?,?,'application/pdf',?,'valid_pdf',?,?)
            """,
            (content_id, 1000, len(pages), timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO parse_runs(
              id,content_sha256,parser_name,parser_version,status,
              created_at,updated_at,completed_at)
            VALUES(?,?,'pymupdf','1.0','completed',?,?,?)
            """,
            (run_id, content_id, timestamp, timestamp, timestamp),
        )
        for page in pages:
            connection.execute(
                """
                INSERT INTO pages(
                  content_sha256,page_index,physical_page,parse_run_id,
                  normalized_text,raw_text,width_points,height_points,
                  blocks_json,coordinate_reliable)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    content_id,
                    page.page_index,
                    page.physical_page,
                    run_id,
                    page.normalized_text,
                    page.raw_text,
                    page.width_points,
                    page.height_points,
                    json.dumps(
                        [block.model_dump(mode="json") for block in page.blocks],
                        ensure_ascii=False,
                    ),
                    1,
                ),
            )
        connection.execute(
            "UPDATE contents SET current_parse_run_id=?,updated_at=? WHERE sha256=?",
            (run_id, timestamp, content_id),
        )
    return run_id
