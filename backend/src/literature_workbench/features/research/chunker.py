"""Chunk parsed pages into retrieval-sized fragments.

The chunker is a pure function over ``ParsedPage`` objects: blocks are
already in column-aware reading order (see ``parser/reading_order.py``),
so greedy in-order merging preserves narrative flow.  Chunks never cross
a page boundary, which keeps every chunk citable by physical page.

Every chunk also carries the ``bbox_1000`` rects of the blocks its text
came from, so a citation can be highlighted on the page instead of just
naming the page number.  Windows cut from one oversized block all share
that block's rect.
"""

from __future__ import annotations

from dataclasses import dataclass

from literature_workbench.features.parser.models import ParsedPage

DEFAULT_TARGET_CHARS = 1000
DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150

# bbox_1000: page-relative (x0, y0, x1, y1) on a 0–1000 grid.
BBox = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class RagChunk:
    physical_page: int
    chunk_index: int
    text: str
    rects: tuple[BBox, ...] = ()


def chunk_pages(
    pages: list[ParsedPage],
    *,
    target_chars: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[RagChunk]:
    resolved_target = DEFAULT_TARGET_CHARS if target_chars is None else target_chars
    if not 0 < resolved_target <= max_chars:
        raise ValueError("rag_chunk_size_invalid")
    if not 0 <= overlap_chars < resolved_target:
        raise ValueError("rag_chunk_overlap_invalid")
    chunks: list[RagChunk] = []
    for page in pages:
        for text, bbox in _page_segments(page, max_chars=max_chars, overlap_chars=overlap_chars):
            _append_segment(chunks, page.physical_page, text, bbox, resolved_target, max_chars)
    return chunks


def _page_segments(
    page: ParsedPage, *, max_chars: int, overlap_chars: int
) -> list[tuple[str, BBox]]:
    segments: list[tuple[str, BBox]] = []
    for block in page.blocks:
        if block.text is None:
            continue
        text = " ".join(block.text.split())
        if not text:
            continue
        bbox: BBox = (
            int(block.bbox_1000[0]),
            int(block.bbox_1000[1]),
            int(block.bbox_1000[2]),
            int(block.bbox_1000[3]),
        )
        if len(text) <= max_chars:
            segments.append((text, bbox))
            continue
        # Oversized block: emit overlapping windows so no context is lost
        # at the split point.
        start = 0
        step = max_chars - overlap_chars
        while start < len(text):
            segments.append((text[start : start + max_chars], bbox))
            start += step
    return segments


def _append_segment(
    chunks: list[RagChunk],
    physical_page: int,
    segment: str,
    bbox: BBox,
    target_chars: int,
    max_chars: int,
) -> None:
    if chunks and chunks[-1].physical_page == physical_page:
        previous = chunks[-1]
        if len(previous.text) < target_chars:
            combined = f"{previous.text} {segment}"
            if len(combined) <= max_chars:
                rects = previous.rects
                if not rects or rects[-1] != bbox:
                    rects = (*rects, bbox)
                chunks[-1] = RagChunk(physical_page, previous.chunk_index, combined, rects)
                return
    chunks.append(RagChunk(physical_page, len(chunks), segment, (bbox,)))
