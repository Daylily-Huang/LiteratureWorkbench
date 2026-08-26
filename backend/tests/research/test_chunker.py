from __future__ import annotations

from uuid import uuid4

from literature_workbench.features.parser.models import ParsedBlock, ParsedPage
from literature_workbench.features.research.chunker import chunk_pages


def _block(text: str, bbox: tuple[int, int, int, int] = (10, 20, 300, 80)) -> ParsedBlock:
    return ParsedBlock(
        block_id=f"0:{hash(text) % 1000}",
        kind="text",
        text=text,
        bbox_1000=bbox,
        source="pymupdf",
        confidence=None,
    )


def _page(physical_page: int, block_texts: list[str]) -> ParsedPage:
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
        blocks=[_block(text) for text in block_texts],
    )


def test_blocks_merge_greedily_up_to_max_chars() -> None:
    pages = [_page(1, ["a" * 500, "b" * 400, "c" * 400])]
    chunks = chunk_pages(pages, max_chars=1000)
    assert len(chunks) == 2
    assert chunks[0].text == "a" * 500 + " " + "b" * 400
    assert chunks[1].text == "c" * 400
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]


def test_chunks_never_cross_page_boundaries() -> None:
    pages = [_page(1, ["a" * 800]), _page(2, ["b" * 100])]
    chunks = chunk_pages(pages, max_chars=1000)
    assert len(chunks) == 2
    assert chunks[0].physical_page == 1
    assert chunks[1].physical_page == 2


def test_oversized_block_splits_with_overlap() -> None:
    text = "x" * 1000 + "MIDDLE" + "y" * 1000
    pages = [_page(1, [text])]
    chunks = chunk_pages(pages, max_chars=1000, overlap_chars=150)
    assert len(chunks) >= 3
    # Overlap: the end of one window reappears at the start of the next.
    for first, second in zip(chunks, chunks[1:], strict=False):
        assert first.text[-150:] == second.text[:150]
    # No content lost.
    reconstructed = chunks[0].text + "".join(c.text[150:] for c in chunks[1:])
    assert reconstructed == text


def test_blank_and_none_blocks_are_skipped() -> None:
    page = _page(1, ["   ", "real content"])
    page.blocks.insert(0, _block("\n\n"))
    chunks = chunk_pages([page], max_chars=1000)
    assert [chunk.text for chunk in chunks] == ["real content"]


def test_pages_without_blocks_produce_no_chunks() -> None:
    assert chunk_pages([_page(1, []), _page(2, [])]) == []


def test_chunk_index_is_global_and_monotonic() -> None:
    pages = [_page(1, ["a" * 900, "b" * 900]), _page(2, ["c" * 10])]
    chunks = chunk_pages(pages, max_chars=1000)
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]
    assert chunks[2].physical_page == 2


def test_invalid_sizing_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="rag_chunk_size_invalid"):
        chunk_pages([], max_chars=0)
    with pytest.raises(ValueError, match="rag_chunk_overlap_invalid"):
        chunk_pages([], target_chars=100, max_chars=200, overlap_chars=100)


def _page_with_bboxes(
    physical_page: int, items: list[tuple[str, tuple[int, int, int, int]]]
) -> ParsedPage:
    page = _page(physical_page, [text for text, _ in items])
    page.blocks.clear()
    for text, bbox in items:
        page.blocks.append(_block(text, bbox))
    return page


def test_chunks_carry_block_rects() -> None:
    page = _page_with_bboxes(
        1, [("alpha", (10, 20, 300, 80)), ("beta", (10, 90, 300, 150))]
    )
    chunks = chunk_pages([page], max_chars=1000)
    assert len(chunks) == 1
    # Merged chunk accumulates each contributing block's bbox in order.
    assert chunks[0].rects == ((10, 20, 300, 80), (10, 90, 300, 150))


def test_merged_chunk_dedupes_consecutive_identical_rects() -> None:
    page = _page_with_bboxes(
        1, [("alpha", (10, 20, 300, 80)), ("beta", (10, 20, 300, 80))]
    )
    chunks = chunk_pages([page], max_chars=1000)
    assert chunks[0].rects == ((10, 20, 300, 80),)


def test_oversized_block_windows_share_one_rect() -> None:
    text = "x" * 1500
    chunks = chunk_pages([_page(1, [text])], max_chars=1000, overlap_chars=150)
    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.rects == ((10, 20, 300, 80),)
