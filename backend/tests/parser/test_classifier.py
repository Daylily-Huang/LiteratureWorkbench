from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import fitz
import pytest

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.classifier import (
    InvalidStagedPdf,
    classify_staged_pdf,
)
from literature_workbench.features.parser.models import StagedPdf


def _staged(path: Path) -> StagedPdf:
    return StagedPdf(
        content_id=ContentId("a" * 64),
        attempt_id=uuid4(),
        path=path,
        sha256="a" * 64,
        page_count=None,
    )


def _write_pdf(path: Path, usable_pages: int, blank_pages: int) -> None:
    document = fitz.open()
    try:
        for index in range(usable_pages + blank_pages):
            page = document.new_page()
            if index < usable_pages:
                page.insert_textbox(
                    fitz.Rect(36, 36, 560, 780),
                    ("usable academic text with enough letters for direct extraction " * 3),
                    fontsize=10,
                )
        document.save(path)
    finally:
        document.close()


def test_ordinary_text_pdf_uses_direct_extraction(tmp_path: Path) -> None:
    path = tmp_path / "ordinary.pdf"
    _write_pdf(path, usable_pages=2, blank_pages=0)

    result = classify_staged_pdf(_staged(path))

    assert result == "pymupdf"


def test_image_only_and_blank_pdfs_use_mineru(tmp_path: Path) -> None:
    image_only = tmp_path / "image-only.pdf"
    blank = tmp_path / "blank.pdf"
    _write_pdf(image_only, usable_pages=0, blank_pages=2)
    _write_pdf(blank, usable_pages=0, blank_pages=1)

    assert classify_staged_pdf(_staged(image_only)) == "mineru"
    assert classify_staged_pdf(_staged(blank)) == "mineru"


@pytest.mark.parametrize(
    ("usable_pages", "expected"),
    [(5, "pymupdf"), (4, "mineru")],
)
def test_mixed_first_eight_pages_use_exact_sixty_percent_threshold(
    tmp_path: Path,
    usable_pages: int,
    expected: str,
) -> None:
    path = tmp_path / f"mixed-{usable_pages}.pdf"
    _write_pdf(path, usable_pages=usable_pages, blank_pages=8 - usable_pages)

    assert classify_staged_pdf(_staged(path)) == expected


def test_classifier_inspects_at_most_first_eight_pages(tmp_path: Path) -> None:
    path = tmp_path / "nine-pages.pdf"
    _write_pdf(path, usable_pages=5, blank_pages=4)

    assert classify_staged_pdf(_staged(path)) == "pymupdf"


def test_corrupt_staging_pdf_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"not a pdf")

    with pytest.raises(InvalidStagedPdf):
        classify_staged_pdf(_staged(path))


def test_replacement_glyph_dominated_text_uses_mineru(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def get_text(self, kind: str) -> str:
            assert kind == "text"
            return ("\ufffd" * 20) + ("a" * 80)

    class FakeDocument:
        page_count = 1
        needs_pass = False

        def __enter__(self) -> FakeDocument:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def load_page(self, index: int) -> FakePage:
            assert index == 0
            return FakePage()

    monkeypatch.setattr(
        "literature_workbench.features.parser.classifier.fitz.open",
        lambda _: FakeDocument(),
    )

    assert classify_staged_pdf(_staged(tmp_path / "owned.pdf")) == "mineru"
