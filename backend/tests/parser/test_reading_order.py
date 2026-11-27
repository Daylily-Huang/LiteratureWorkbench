from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import fitz  # type: ignore[import-untyped]

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.models import StagedPdf
from literature_workbench.features.parser.normalizer import extract_pymupdf_pages
from literature_workbench.features.parser.reading_order import reading_order_permutation

PAGE_WIDTH = 595.0


def test_two_column_page_reads_left_column_then_right_column() -> None:
    # Mirrors the silver-backed chevrotain page-1 geometry (points).
    bboxes = [
        (325.0, 26.0, 545.0, 52.0),  # 0 header (right area, above title)
        (395.0, 52.0, 544.0, 62.0),  # 1 header doi
        (42.0, 96.0, 498.0, 183.0),  # 2 title (spans the gutter)
        (42.0, 195.0, 547.0, 243.0),  # 3 authors (spans the gutter)
        (304.0, 263.0, 555.0, 286.0),  # 4 right column
        (42.0, 263.0, 294.0, 348.0),  # 5 left column
        (304.0, 284.0, 555.0, 465.0),  # 6 right column
        (42.0, 347.0, 294.0, 528.0),  # 7 left column
        (304.0, 462.0, 555.0, 622.0),  # 8 right column
        (304.0, 620.0, 555.0, 685.0),  # 9 right column
        (42.0, 525.0, 294.0, 685.0),  # 10 left column
        (42.0, 706.0, 552.0, 736.0),  # 11 affiliations (spanning)
        (42.0, 736.0, 364.0, 746.0),  # 12 footnote (crosses the gutter)
        (42.0, 757.0, 229.0, 766.0),  # 13 footer (inside the left band)
    ]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == [
        0,
        1,
        2,
        3,
        5,
        7,
        10,
        4,
        6,
        8,
        9,
        11,
        12,
        13,
    ]


def test_single_column_keeps_extraction_order() -> None:
    bboxes = [(42.0, 50.0 + index * 60.0, 550.0, 100.0 + index * 60.0) for index in range(5)]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == list(range(5))


def test_unbalanced_side_by_side_blocks_do_not_form_columns() -> None:
    bboxes = [
        (42.0, 100.0, 294.0, 180.0),
        (42.0, 190.0, 294.0, 270.0),
        (42.0, 280.0, 294.0, 360.0),
        (304.0, 100.0, 555.0, 180.0),  # lone right block: no vertical stack
    ]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == list(range(4))


def test_narrow_gap_is_not_a_column_gutter() -> None:
    bboxes = [
        (42.0, 100.0, 294.0, 180.0),
        (42.0, 190.0, 294.0, 270.0),
        (297.0, 100.0, 555.0, 180.0),  # 3-point gap: below the gutter minimum
        (297.0, 190.0, 555.0, 270.0),
    ]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == list(range(4))


def test_broken_gutter_run_is_not_a_column_gutter() -> None:
    # A dominant wide element crosses the strip; the remaining clear runs
    # are each shorter than the minimum run fraction of the text extent.
    bboxes = [
        (42.0, 100.0, 294.0, 140.0),
        (42.0, 145.0, 294.0, 180.0),
        (304.0, 100.0, 555.0, 140.0),
        (304.0, 145.0, 555.0, 180.0),
        (42.0, 200.0, 555.0, 620.0),  # tall spanning element dominates the page
        (42.0, 640.0, 294.0, 665.0),
        (304.0, 640.0, 555.0, 665.0),
        (42.0, 670.0, 294.0, 695.0),
        (304.0, 670.0, 555.0, 695.0),
    ]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == list(range(9))


def test_wide_heading_splits_columns_into_sections() -> None:
    bboxes = [
        (42.0, 100.0, 294.0, 180.0),  # 0 left 1
        (304.0, 100.0, 555.0, 180.0),  # 1 right 1
        (42.0, 190.0, 294.0, 270.0),  # 2 left 2
        (304.0, 190.0, 555.0, 270.0),  # 3 right 2
        (42.0, 300.0, 555.0, 330.0),  # 4 spanning heading
        (42.0, 340.0, 294.0, 420.0),  # 5 left 3
        (304.0, 340.0, 555.0, 420.0),  # 6 right 3
    ]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == [0, 2, 1, 3, 4, 5, 6]


def test_three_columns_read_left_to_right() -> None:
    bboxes = [
        (42.0, 100.0, 210.0, 180.0),  # 0 column 1 top
        (225.0, 100.0, 390.0, 180.0),  # 1 column 2 top
        (405.0, 100.0, 555.0, 180.0),  # 2 column 3 top
        (42.0, 190.0, 210.0, 270.0),  # 3 column 1 bottom
        (225.0, 190.0, 390.0, 270.0),  # 4 column 2 bottom
        (405.0, 190.0, 555.0, 270.0),  # 5 column 3 bottom
    ]
    assert reading_order_permutation(bboxes, PAGE_WIDTH) == [0, 3, 1, 4, 2, 5]


def test_few_blocks_return_identity() -> None:
    assert reading_order_permutation([(0.0, 0.0, 100.0, 50.0)], PAGE_WIDTH) == [0]
    assert reading_order_permutation([], PAGE_WIDTH) == []


def _sentences(marker: str) -> str:
    return (
        f"{marker} sentinel paragraph with enough words to wrap across several "
        "lines of the narrow column so that pymupdf forms a proper text block "
        "here. " * 3
    )


def _write_two_column_pdf(path: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=595, height=790)
        page.insert_textbox(
            fitz.Rect(42, 60, 550, 110),
            "Sentinel Title Spanning Both Columns Of The Page",
            fontsize=14,
        )
        page.insert_textbox(fitz.Rect(42, 150, 294, 290), _sentences("Alpha lefttop"), fontsize=10)
        page.insert_textbox(fitz.Rect(42, 310, 294, 450), _sentences("Bravo leftbottom"), fontsize=10)
        page.insert_textbox(fitz.Rect(304, 150, 555, 290), _sentences("Charlie righttop"), fontsize=10)
        page.insert_textbox(fitz.Rect(304, 310, 555, 450), _sentences("Delta rightbottom"), fontsize=10)
        document.save(path)


def _staged(path: Path) -> StagedPdf:
    payload = path.read_bytes()
    return StagedPdf(
        content_id=ContentId(hashlib.sha256(payload).hexdigest()),
        attempt_id=uuid4(),
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        page_count=1,
    )


def test_extract_pymupdf_pages_reads_two_columns_in_reading_order(tmp_path: Path) -> None:
    pdf_path = tmp_path / "two_column.pdf"
    _write_two_column_pdf(pdf_path)
    pages = extract_pymupdf_pages(_staged(pdf_path), uuid4())
    assert len(pages) == 1
    page = pages[0]
    block_text = "\n".join(block.text or "" for block in page.blocks)
    for label, text in (("normalized", page.normalized_text), ("raw", page.raw_text), ("blocks", block_text)):
        title = text.index("Sentinel Title")
        alpha = text.index("Alpha lefttop")
        bravo = text.index("Bravo leftbottom")
        charlie = text.index("Charlie righttop")
        delta = text.index("Delta rightbottom")
        assert title < alpha < bravo < charlie < delta, label
    assert [block.block_id for block in page.blocks] == [f"0:{index}" for index in range(5)]


def test_extract_pymupdf_pages_single_column_raw_text_matches_pymupdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "single_column.pdf"
    with fitz.open() as document:
        page = document.new_page(width=400, height=600)
        page.insert_textbox(
            fitz.Rect(20, 20, 380, 580),
            "direct extraction academic page text " * 5,
            fontsize=10,
        )
        document.save(pdf_path)
    pages = extract_pymupdf_pages(_staged(pdf_path), uuid4())
    assert len(pages) == 1
    with fitz.open(pdf_path) as document:
        expected_raw = str(document.load_page(0).get_text("text", sort=True))
    assert pages[0].raw_text == expected_raw
