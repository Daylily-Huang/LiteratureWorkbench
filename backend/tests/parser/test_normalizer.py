from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from conftest import ParserLibraryFixture
from literature_workbench.features.parser.models import ParsedBlock, ParsedPage
from literature_workbench.features.parser.normalizer import ParseRunRepository


class RejectLease:
    def require_current_lease(self, connection: object) -> None:
        assert connection.in_transaction  # type: ignore[attr-defined]
        raise RuntimeError("lost_test_lease")


def _block(source: str = "pymupdf") -> ParsedBlock:
    return ParsedBlock(
        block_id="0:0",
        kind="text",
        text="raw block",
        bbox_1000=(10, 20, 300, 80),
        source=source,  # type: ignore[arg-type]
        confidence=None if source == "pymupdf" else 0.85,
    )


def _page(
    content_id: str,
    run_id: UUID,
    page_index: int,
    parser: str,
    text: str,
) -> ParsedPage:
    return ParsedPage(
        content_id=content_id,  # type: ignore[arg-type]
        page_index=page_index,
        physical_page=page_index + 1,
        parse_run_id=run_id,
        parser_name=parser,  # type: ignore[arg-type]
        parser_version=f"{parser}-1",
        normalized_text=text,
        raw_text=text,
        width_points=400,
        height_points=600,
        coordinate_reliable=True,
        blocks=[] if text == "" else [_block("pymupdf" if parser == "pymupdf" else "mineru_ocr")],
    )


def _prepare_content(parser_library: ParserLibraryFixture, payload: bytes, pages: int) -> str:
    registered = parser_library.register("normalizer.pdf", payload)
    with parser_library.database.transaction() as connection:
        connection.execute(
            "UPDATE contents SET page_count=?,status='valid_pdf' WHERE sha256=?",
            (pages, registered.content_id),
        )
    return str(registered.content_id)


def _create_run(
    parser_library: ParserLibraryFixture,
    repository: ParseRunRepository,
    content_id: str,
    parser_name: str,
    parser_version: str,
    **kwargs: object,
) -> UUID:
    with parser_library.database.transaction() as connection:
        allocated = connection.execute(
            """
            UPDATE contents SET parse_request_revision=parse_request_revision+1
            WHERE sha256=? RETURNING parse_request_revision
            """,
            (content_id,),
        ).fetchone()
    assert allocated is not None
    return repository.create_run(
        content_id,
        parser_name,
        parser_version,
        request_revision=int(allocated["parse_request_revision"]),
        **kwargs,
    )


def test_pymupdf_and_mineru_pages_round_trip_exactly_including_blank(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-normalizer", 2)
    repository = ParseRunRepository(parser_library.database)

    direct_run = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")
    direct_pages = [
        _page(content_id, direct_run, 0, "pymupdf", "direct text"),
        _page(content_id, direct_run, 1, "pymupdf", ""),
    ]
    repository.complete_run(direct_run, direct_pages)
    assert repository.read_current_page(content_id, 1) == direct_pages[0]
    assert repository.read_current_page(content_id, 2) == direct_pages[1]

    mineru_run = _create_run(parser_library, repository, content_id, "mineru", "mineru-1")
    mineru_pages = [
        _page(content_id, mineru_run, 0, "mineru", "ocr text"),
        _page(content_id, mineru_run, 1, "mineru", ""),
    ]
    repository.complete_run(mineru_run, mineru_pages)
    assert repository.read_current_page(content_id, 1) == mineru_pages[0]
    assert repository.read_current_page(content_id, 2) == mineru_pages[1]


def test_failed_retry_preserves_old_completed_pointer_and_pages(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-old-pointer", 2)
    repository = ParseRunRepository(parser_library.database)
    first = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")
    old_pages = [
        _page(content_id, first, 0, "pymupdf", "old page one"),
        _page(content_id, first, 1, "pymupdf", "old page two"),
    ]
    repository.complete_run(first, old_pages)
    failed = _create_run(parser_library, repository, content_id, "mineru", "mineru-1")

    repository.fail_run(failed, "mineru_invalid_output")

    assert repository.current_run_id(content_id) == first
    assert repository.read_current_page(content_id, 1) == old_pages[0]
    assert repository.read_current_page(content_id, 2) == old_pages[1]


def test_second_success_switches_pointer_atomically_without_mixed_pages(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-new-pointer", 2)
    repository = ParseRunRepository(parser_library.database)
    first = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")
    repository.complete_run(
        first,
        [
            _page(content_id, first, 0, "pymupdf", "old one"),
            _page(content_id, first, 1, "pymupdf", "old two"),
        ],
    )
    second = _create_run(parser_library, repository, content_id, "mineru", "mineru-1")
    new_pages = [
        _page(content_id, second, 0, "mineru", "new one"),
        _page(content_id, second, 1, "mineru", "new two"),
    ]

    repository.complete_run(second, new_pages)

    assert repository.current_run_id(content_id) == second
    assert repository.read_current_page(content_id, 1) == new_pages[0]
    assert repository.read_current_page(content_id, 2) == new_pages[1]
    with parser_library.database.connect() as connection:
        old_count = connection.execute(
            "SELECT COUNT(*) AS count FROM pages WHERE parse_run_id=?", (str(first),)
        ).fetchone()
    assert old_count is not None
    assert int(old_count["count"]) == 2


def test_incomplete_attempt_cannot_switch_pointer(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-incomplete", 2)
    repository = ParseRunRepository(parser_library.database)
    run_id = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")

    with pytest.raises(ValueError, match="parse_pages_incomplete"):
        repository.complete_run(
            run_id,
            [_page(content_id, run_id, 0, "pymupdf", "only one")],
        )

    assert repository.current_run_id(content_id) is None
    with parser_library.database.connect() as connection:
        page_count = connection.execute(
            "SELECT COUNT(*) AS count FROM pages WHERE parse_run_id=?", (str(run_id),)
        ).fetchone()
    assert page_count is not None
    assert int(page_count["count"]) == 0


def test_lost_lease_inside_completion_transaction_has_zero_page_or_pointer_effect(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-lease-fence", 1)
    repository = ParseRunRepository(parser_library.database)
    run_id = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")

    with pytest.raises(RuntimeError, match="lost_test_lease"):
        repository.complete_run(
            run_id,
            [_page(content_id, run_id, 0, "pymupdf", "must roll back")],
            cancellation=RejectLease(),
        )

    assert repository.current_run_id(content_id) is None
    with parser_library.database.connect() as connection:
        pages = connection.execute(
            "SELECT COUNT(*) AS count FROM pages WHERE parse_run_id=?", (str(run_id),)
        ).fetchone()
        run = connection.execute(
            "SELECT status FROM parse_runs WHERE id=?", (str(run_id),)
        ).fetchone()
    assert pages is not None and int(pages["count"]) == 0
    assert run is not None and run["status"] == "running"


def test_expensive_input_verification_runs_once_before_completion_write_lock(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-preflight-hash", 1)
    repository = ParseRunRepository(parser_library.database)
    run_id = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")
    calls = 0

    def verify_inputs() -> None:
        nonlocal calls
        calls += 1
        with parser_library.database.connect() as connection:
            connection.execute("PRAGMA busy_timeout=1")
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()

    repository.complete_run(
        run_id,
        [_page(content_id, run_id, 0, "pymupdf", "verified page")],
        verify_inputs=verify_inputs,
    )

    assert calls == 1
    assert repository.current_run_id(content_id) == run_id


def test_older_concurrent_run_cannot_overwrite_newer_published_run(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-concurrent", 1)
    repository = ParseRunRepository(parser_library.database)
    older = _create_run(parser_library, repository, content_id, "pymupdf", "pymupdf-1")
    newer = _create_run(parser_library, repository, content_id, "mineru", "mineru-1")
    repository.complete_run(
        newer, [_page(content_id, newer, 0, "mineru", "new publication")]
    )

    with pytest.raises(ValueError, match="stale_parse_run"):
        repository.complete_run(
            older, [_page(content_id, older, 0, "pymupdf", "stale publication")]
        )

    assert repository.current_run_id(content_id) == newer
    current = repository.read_current_page(content_id, 1)
    assert current is not None and current.normalized_text == "new publication"


def test_older_request_cannot_start_after_newer_request_revision_was_allocated(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-request-order", 1)
    repository = ParseRunRepository(parser_library.database)
    with parser_library.database.transaction() as connection:
        connection.execute(
            "UPDATE contents SET parse_request_revision=2 WHERE sha256=?",
            (content_id,),
        )

    newer = repository.create_run(
        content_id,
        "pymupdf",
        "pymupdf-1",
        request_revision=2,
    )
    repository.complete_run(
        newer, [_page(content_id, newer, 0, "pymupdf", "newer request")]
    )

    with pytest.raises(ValueError, match="stale_parse_request"):
        repository.create_run(
            content_id,
            "pymupdf",
            "pymupdf-1",
            request_revision=1,
        )

    assert repository.current_run_id(content_id) == newer


def test_create_run_and_set_output_dir_are_lease_fenced_after_begin_immediate(
    parser_library: ParserLibraryFixture,
) -> None:
    content_id = _prepare_content(parser_library, b"%PDF-write-fences", 1)
    repository = ParseRunRepository(parser_library.database)

    with pytest.raises(RuntimeError, match="lost_test_lease"):
        _create_run(
            parser_library,
            repository,
            content_id,
            "mineru",
            "mineru-1",
            cancellation=RejectLease(),
        )
    with parser_library.database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM parse_runs WHERE content_sha256=?", (content_id,)
        ).fetchone()
    assert count is not None and int(count["count"]) == 0

    run_id = _create_run(parser_library, repository, content_id, "mineru", "mineru-1")
    with pytest.raises(RuntimeError, match="lost_test_lease"):
        repository.set_output_dir(
            run_id,
            f"parse-results/{content_id}/{run_id}",
            cancellation=RejectLease(),
        )
    with parser_library.database.connect() as connection:
        run = connection.execute(
            "SELECT output_dir FROM parse_runs WHERE id=?", (str(run_id),)
        ).fetchone()
    assert run is not None and run["output_dir"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width_points": float("nan")},
        {"height_points": 0},
        {"physical_page": 2},
    ],
)
def test_parsed_page_rejects_invalid_dimensions_and_physical_mapping(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "content_id": "a" * 64,
        "page_index": 0,
        "physical_page": 1,
        "parse_run_id": uuid4(),
        "parser_name": "pymupdf",
        "parser_version": "v",
        "normalized_text": "",
        "raw_text": "",
        "width_points": 400,
        "height_points": 600,
        "coordinate_reliable": True,
        "blocks": [],
    }
    values.update(kwargs)

    with pytest.raises(ValidationError):
        ParsedPage.model_validate(values)


@pytest.mark.parametrize(
    ("bbox", "confidence"),
    [
        ((-1, 0, 1, 1), None),
        ((10, 0, 1, 1), None),
        ((0, 0, 1, 1), 1.1),
        ((0, 0, float("inf"), 1), None),
    ],
)
def test_parsed_block_rejects_invalid_bbox_or_confidence(
    bbox: tuple[float, float, float, float],
    confidence: float | None,
) -> None:
    with pytest.raises(ValidationError):
        ParsedBlock(
            block_id="bad",
            kind="text",
            text=None,
            bbox_1000=bbox,
            source="mineru_ocr",
            confidence=confidence,
        )
