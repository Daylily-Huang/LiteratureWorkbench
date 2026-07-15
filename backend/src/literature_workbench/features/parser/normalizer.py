from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]

from literature_workbench.db.database import Database
from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.models import ParsedBlock, ParsedPage, StagedPdf


class ParseRunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_run(
        self,
        content_id: ContentId | str,
        parser_name: str,
        parser_version: str,
        *,
        output_dir: str | None = None,
        cancellation: object | None = None,
        source_job_id: UUID | str | None = None,
        request_revision: int,
    ) -> UUID:
        if parser_name not in {"pymupdf", "mineru"}:
            raise ValueError("invalid_parser_name")
        if request_revision <= 0:
            raise ValueError("invalid_parse_request_revision")
        run_id = uuid4()
        timestamp = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            _fence(cancellation, connection)
            content = connection.execute(
                "SELECT parse_request_revision FROM contents WHERE sha256=?",
                (str(content_id),),
            ).fetchone()
            if content is None:
                raise ValueError("parse_content_not_found")
            if int(content["parse_request_revision"]) != request_revision:
                raise ValueError("stale_parse_request")
            connection.execute(
                """
                INSERT INTO parse_runs(
                  id,content_sha256,parser_name,parser_version,status,output_dir,
                  request_revision,source_job_id,created_at,updated_at)
                VALUES(?,?,?,?, 'running',?,?,?,?,?)
                """,
                (
                    str(run_id),
                    str(content_id),
                    parser_name,
                    parser_version,
                    output_dir,
                    request_revision,
                    None if source_job_id is None else str(source_job_id),
                    timestamp,
                    timestamp,
                ),
            )
        return run_id

    def complete_run(
        self,
        run_id: UUID,
        pages: list[ParsedPage],
        *,
        cancellation: object | None = None,
        verify_inputs: Callable[[], None] | None = None,
    ) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            _fence(cancellation, connection)
            if verify_inputs is not None:
                verify_inputs()
            run = connection.execute(
                "SELECT * FROM parse_runs WHERE id=? AND status='running'",
                (str(run_id),),
            ).fetchone()
            if run is None:
                raise ValueError("parse_run_not_running")
            content_id = str(run["content_sha256"])
            content = connection.execute(
                "SELECT page_count FROM contents WHERE sha256=?", (content_id,)
            ).fetchone()
            expected_count = None if content is None else content["page_count"]
            if expected_count is None or not _pages_form_complete_attempt(
                pages,
                run_id,
                content_id,
                str(run["parser_name"]),
                str(run["parser_version"]),
                int(expected_count),
            ):
                raise ValueError("parse_pages_incomplete")
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
                        str(run_id),
                        page.normalized_text,
                        page.raw_text,
                        page.width_points,
                        page.height_points,
                        json.dumps(
                            [block.model_dump(mode="json") for block in page.blocks],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        int(page.coordinate_reliable),
                    ),
                )
            changed = connection.execute(
                """
                UPDATE parse_runs
                SET status='completed',updated_at=?,completed_at=?,error_code=NULL
                WHERE id=? AND status='running'
                """,
                (timestamp, timestamp, str(run_id)),
            )
            if changed.rowcount != 1:
                raise ValueError("parse_run_completion_conflict")
            _fence(cancellation, connection)
            if verify_inputs is not None:
                verify_inputs()
            pointer = connection.execute(
                """
                UPDATE contents SET current_parse_run_id=?,updated_at=?
                WHERE sha256=? AND parse_request_revision=?
                """,
                (
                    str(run_id),
                    timestamp,
                    content_id,
                    int(run["request_revision"]),
                ),
            )
            if pointer.rowcount != 1:
                raise ValueError("stale_parse_run")

    def fail_run(self, run_id: UUID, error_code: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        with self._database.transaction() as connection:
            run = connection.execute(
                "SELECT content_sha256 FROM parse_runs WHERE id=? AND status='running'",
                (str(run_id),),
            ).fetchone()
            if run is None:
                raise ValueError("parse_run_not_running")
            current = connection.execute(
                "SELECT current_parse_run_id FROM contents WHERE sha256=?",
                (str(run["content_sha256"]),),
            ).fetchone()
            if current is not None and str(current["current_parse_run_id"] or "") != str(run_id):
                connection.execute("DELETE FROM pages WHERE parse_run_id=?", (str(run_id),))
            connection.execute(
                """
                UPDATE parse_runs
                SET status='failed',error_code=?,updated_at=?
                WHERE id=? AND status='running'
                """,
                (error_code, timestamp, str(run_id)),
            )

    def set_output_dir(
        self,
        run_id: UUID,
        relative_output_dir: str,
        *,
        cancellation: object | None = None,
    ) -> None:
        path = PurePosixPath(relative_output_dir)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("parse_output_dir_must_be_relative")
        with self._database.transaction() as connection:
            _fence(cancellation, connection)
            changed = connection.execute(
                "UPDATE parse_runs SET output_dir=? WHERE id=? AND status='running'",
                (path.as_posix(), str(run_id)),
            )
            if changed.rowcount != 1:
                raise ValueError("parse_run_not_running")

    def current_run_id(self, content_id: ContentId | str) -> UUID | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT current_parse_run_id FROM contents WHERE sha256=?",
                (str(content_id),),
            ).fetchone()
        if row is None or row["current_parse_run_id"] is None:
            return None
        return UUID(str(row["current_parse_run_id"]))

    def read_current_page(
        self,
        content_id: ContentId | str,
        physical_page: int,
    ) -> ParsedPage | None:
        if physical_page < 1:
            return None
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.*,r.parser_name,r.parser_version
                FROM contents AS c
                JOIN parse_runs AS r ON r.id=c.current_parse_run_id AND r.status='completed'
                JOIN pages AS p ON p.parse_run_id=r.id AND p.content_sha256=c.sha256
                WHERE c.sha256=? AND p.page_index=?
                """,
                (str(content_id), physical_page - 1),
            ).fetchone()
        if row is None:
            return None
        return _parsed_page_from_row(row)

    def read_document_current_page(
        self,
        document_id: UUID,
        physical_page: int,
    ) -> tuple[bool, ParsedPage | None]:
        """Resolve document existence and its current page in one DB snapshot."""
        if physical_page < 1:
            return False, None
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.id AS requested_document_id,p.*,r.parser_name,r.parser_version
                FROM documents AS d
                LEFT JOIN contents AS c ON c.sha256=d.content_sha256
                LEFT JOIN parse_runs AS r
                  ON r.id=c.current_parse_run_id AND r.status='completed'
                LEFT JOIN pages AS p
                  ON p.parse_run_id=r.id
                 AND p.content_sha256=c.sha256
                 AND p.page_index=?
                WHERE d.id=?
                """,
                (physical_page - 1, str(document_id)),
            ).fetchone()
        if row is None:
            return False, None
        if row["parse_run_id"] is None:
            return True, None
        return True, _parsed_page_from_row(row)


def _parsed_page_from_row(row: sqlite3.Row) -> ParsedPage:
    raw_blocks = json.loads(str(row["blocks_json"]))
    if not isinstance(raw_blocks, list):
        raise ValueError("stored_parse_blocks_invalid")
    return ParsedPage(
        content_id=ContentId(str(row["content_sha256"])),
        page_index=int(row["page_index"]),
        physical_page=int(row["physical_page"]),
        parse_run_id=UUID(str(row["parse_run_id"])),
        parser_name=cast(str, row["parser_name"]),  # type: ignore[arg-type]
        parser_version=str(row["parser_version"]),
        normalized_text=str(row["normalized_text"]),
        raw_text=str(row["raw_text"]),
        width_points=float(row["width_points"]),
        height_points=float(row["height_points"]),
        coordinate_reliable=bool(row["coordinate_reliable"]),
        blocks=[ParsedBlock.model_validate(block) for block in raw_blocks],
    )


def extract_pymupdf_pages(staged: StagedPdf, parse_run_id: UUID) -> list[ParsedPage]:
    if staged.page_count is None or staged.page_count <= 0:
        raise ValueError("staged_page_count_missing")
    pages: list[ParsedPage] = []
    with fitz.open(staged.path) as document:
        if document.page_count != staged.page_count:
            raise ValueError("staged_page_count_changed")
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            width = float(page.rect.width)
            height = float(page.rect.height)
            blocks: list[ParsedBlock] = []
            for ordinal, raw_block in enumerate(page.get_text("blocks", sort=True)):
                x0, y0, x1, y1, text, *_tail = raw_block
                if not isinstance(text, str) or not text.strip():
                    continue
                blocks.append(
                    ParsedBlock(
                        block_id=f"{page_index}:{ordinal}",
                        kind="text",
                        text=text,
                        bbox_1000=(
                            _normalized_coordinate(float(x0), width),
                            _normalized_coordinate(float(y0), height),
                            _normalized_coordinate(float(x1), width),
                            _normalized_coordinate(float(y1), height),
                        ),
                        source="pymupdf",
                        confidence=None,
                    )
                )
            raw_text = str(page.get_text("text", sort=True))
            pages.append(
                ParsedPage(
                    content_id=staged.content_id,
                    page_index=page_index,
                    physical_page=page_index + 1,
                    parse_run_id=parse_run_id,
                    parser_name="pymupdf",
                    parser_version=str(fitz.VersionBind),
                    normalized_text=" ".join(raw_text.split()),
                    raw_text=raw_text,
                    width_points=width,
                    height_points=height,
                    coordinate_reliable=True,
                    blocks=blocks,
                )
            )
    return pages


def _fence(cancellation: object | None, connection: sqlite3.Connection) -> None:
    if cancellation is None:
        return
    cancellation.require_current_lease(connection)  # type: ignore[attr-defined]


def _pages_form_complete_attempt(
    pages: list[ParsedPage],
    run_id: UUID,
    content_id: str,
    parser_name: str,
    parser_version: str,
    expected_count: int,
) -> bool:
    if len(pages) != expected_count:
        return False
    return all(
        page.parse_run_id == run_id
        and str(page.content_id) == content_id
        and page.parser_name == parser_name
        and page.parser_version == parser_version
        and page.page_index == index
        and page.physical_page == index + 1
        for index, page in enumerate(pages)
    )


def _normalized_coordinate(value: float, dimension: float) -> float:
    return max(0.0, min(1000.0, value / dimension * 1000.0))
