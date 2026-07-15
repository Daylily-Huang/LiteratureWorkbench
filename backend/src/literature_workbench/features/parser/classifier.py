from __future__ import annotations

import re
import unicodedata

import fitz  # type: ignore[import-untyped]

from literature_workbench.features.parser.models import ParseStrategy, StagedPdf


MAX_INSPECTED_PAGES = 8
MIN_USABLE_CHARACTERS = 80
MAX_BAD_CHARACTER_RATIO = 0.10
MIN_DIRECT_PAGE_RATIO = 0.60
MAX_LANGUAGE_SAMPLE_CHARACTERS = 4096


class InvalidStagedPdf(ValueError):
    pass


def classify_staged_pdf(staged: StagedPdf) -> ParseStrategy:
    try:
        with fitz.open(staged.path) as document:
            if document.needs_pass or document.page_count <= 0:
                raise InvalidStagedPdf("staged_pdf_not_structurally_readable")
            inspected = min(document.page_count, MAX_INSPECTED_PAGES)
            usable = sum(
                _page_text_is_usable(document.load_page(index).get_text("text"))
                for index in range(inspected)
            )
    except InvalidStagedPdf:
        raise
    except Exception as error:
        raise InvalidStagedPdf("staged_pdf_not_structurally_readable") from error
    return "pymupdf" if usable / inspected >= MIN_DIRECT_PAGE_RATIO else "mineru"


def bounded_body_language_sample(staged: StagedPdf) -> str:
    chunks: list[str] = []
    remaining = MAX_LANGUAGE_SAMPLE_CHARACTERS
    try:
        with fitz.open(staged.path) as document:
            for page_index in range(min(document.page_count, MAX_INSPECTED_PAGES)):
                if remaining <= 0:
                    break
                text = document.load_page(page_index).get_text("text")[:remaining]
                chunks.append(text)
                remaining -= len(text)
    except Exception as error:
        raise InvalidStagedPdf("staged_pdf_language_sample_failed") from error
    return "\n".join(chunks)


def _page_text_is_usable(raw_text: str) -> bool:
    normalized = re.sub(r"\s+", " ", raw_text).strip()
    if len(normalized) < MIN_USABLE_CHARACTERS:
        return False
    bad_characters = sum(
        character == "\ufffd"
        or (unicodedata.category(character).startswith("C") and not character.isspace())
        for character in normalized
    )
    return bad_characters / len(normalized) < MAX_BAD_CHARACTER_RATIO
