"""Column-aware reading order for extracted PDF text blocks.

PyMuPDF's ``sort=True`` orders blocks by ``(y, x)``, which interleaves the
columns of a multi-column page row by row: stored ``normalized_text`` and
``raw_text`` then read one line of the left column followed by one line of
the right column.  This module detects clear column gutters — vertical
strips that stay free of text blocks over a substantial contiguous
vertical run — and reorders blocks column by column.  Blocks that cross a
gutter (titles, section headings, footnotes) are treated as
section breaks: column content above such a block is read first (left
column top-to-bottom, then the next column), then the spanning block,
then the content below it.

The detector is deliberately conservative.  Pages without a clear gutter
structure (single-column articles, sparse pages, pages where a figure
breaks every candidate strip) keep the original extraction order, so
existing single-column documents are byte-identical before and after.
"""

from __future__ import annotations

BBox = tuple[float, float, float, float]

_MIN_GUTTER_WIDTH_POINTS = 6.0
_MIN_GUTTER_WIDTH_FRACTION = 0.012
_MIN_GUTTER_RUN_FRACTION = 0.2
_MIN_SIDE_BLOCKS = 2
_MIN_SIDE_COVER_FRACTION = 0.5
_MAX_GUTTERS = 3
_EDGE_TOLERANCE_POINTS = 0.5


def reading_order_permutation(bboxes: list[BBox], page_width: float) -> list[int]:
    """Return block indices reordered into column-aware reading order.

    ``bboxes`` are ``(x0, y0, x1, y1)`` in PDF points.  The identity
    permutation is returned whenever no clear multi-column gutter
    structure is found.
    """
    gutters = _column_gutters(bboxes, page_width)
    if not gutters:
        return list(range(len(bboxes)))
    edges = [0.0]
    for gutter_left, gutter_right in gutters:
        edges.extend((gutter_left, gutter_right))
    edges.append(page_width)
    bands = [(edges[index], edges[index + 1]) for index in range(0, len(edges), 2)]
    spanning: list[int] = []
    band_members: list[list[int]] = [[] for _band in bands]
    for index, (x0, _y0, x1, _y1) in enumerate(bboxes):
        band = next(
            (
                position
                for position, (left, right) in enumerate(bands)
                if x0 >= left - _EDGE_TOLERANCE_POINTS
                and x1 <= right + _EDGE_TOLERANCE_POINTS
            ),
            None,
        )
        if band is None:
            spanning.append(index)
        else:
            band_members[band].append(index)
    spanning.sort(key=lambda index: (bboxes[index][1], bboxes[index][0]))
    sections: list[list[list[int]]] = [
        [[] for _band in bands] for _spanning in range(len(spanning) + 1)
    ]
    for band_index, members in enumerate(band_members):
        for index in members:
            section = sum(
                1
                for spanning_index in spanning
                if bboxes[spanning_index][1] <= bboxes[index][1]
            )
            sections[section][band_index].append(index)
    order: list[int] = []
    for section, section_bands in enumerate(sections):
        for members in section_bands:
            order.extend(
                sorted(members, key=lambda index: (bboxes[index][1], bboxes[index][0]))
            )
        if section < len(spanning):
            order.append(spanning[section])
    return order


def _column_gutters(bboxes: list[BBox], page_width: float) -> list[tuple[float, float]]:
    if len(bboxes) < 4:
        return []
    text_top = min(bbox[1] for bbox in bboxes)
    text_bottom = max(bbox[3] for bbox in bboxes)
    text_extent = text_bottom - text_top
    if text_extent <= 0:
        return []
    min_width = max(
        _MIN_GUTTER_WIDTH_POINTS, page_width * _MIN_GUTTER_WIDTH_FRACTION
    )
    min_run = text_extent * _MIN_GUTTER_RUN_FRACTION
    left_edges = sorted({bbox[2] for bbox in bboxes} | {0.0})
    right_edges = sorted({bbox[0] for bbox in bboxes} | {page_width})
    # A candidate strip is defined by a left block edge and a right block
    # edge.  Only blocks *crossing* the strip can interrupt its clear
    # vertical run; blocks that merely sit left or right of it never do.
    # Trying every edge pair lets each logical gutter be found at its
    # tightest width, which is also where the side-stacking checks pass.
    qualified: list[tuple[float, float, float]] = []
    for gutter_left in left_edges:
        for gutter_right in right_edges:
            if gutter_right - gutter_left < min_width:
                continue
            crossing = [
                bbox
                for bbox in bboxes
                if bbox[0] < gutter_right and bbox[2] > gutter_left
            ]
            intervals = sorted((bbox[1], bbox[3]) for bbox in crossing)
            merged: list[tuple[float, float]] = []
            for interval_top, interval_bottom in intervals:
                if merged and interval_top <= merged[-1][1]:
                    merged[-1] = (
                        merged[-1][0],
                        max(merged[-1][1], interval_bottom),
                    )
                else:
                    merged.append((interval_top, interval_bottom))
            cursor = text_top
            run_length = 0.0
            run: tuple[float, float] | None = None
            for interval_top, interval_bottom in merged:
                if interval_top > cursor and interval_top - cursor > run_length:
                    run_length = interval_top - cursor
                    run = (cursor, interval_top)
                cursor = max(cursor, interval_bottom)
            if text_bottom - cursor > run_length:
                run_length = text_bottom - cursor
                run = (cursor, text_bottom)
            if run is None or run_length < min_run:
                continue
            if not _sides_cover_run(
                bboxes, gutter_left, gutter_right, run[0], run[1]
            ):
                continue
            qualified.append((run_length, gutter_left, gutter_right))
    # Highest-scoring first; drop strips that x-overlap an already chosen
    # gutter (the same logical gutter detected at slightly different widths).
    qualified.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[float, float]] = []
    for _length, gutter_left, gutter_right in qualified:
        if any(
            gutter_left < chosen_right and chosen_left < gutter_right
            for chosen_left, chosen_right in chosen
        ):
            continue
        chosen.append((gutter_left, gutter_right))
        if len(chosen) >= _MAX_GUTTERS:
            break
    return sorted(chosen)


def _sides_cover_run(
    bboxes: list[BBox],
    gutter_left: float,
    gutter_right: float,
    run_top: float,
    run_bottom: float,
) -> bool:
    run_length = run_bottom - run_top
    if run_length <= 0:
        return False
    for is_left in (True, False):
        members = [
            bbox
            for bbox in bboxes
            if (
                bbox[2] <= gutter_left + _EDGE_TOLERANCE_POINTS
                if is_left
                else bbox[0] >= gutter_right - _EDGE_TOLERANCE_POINTS
            )
            and bbox[3] > run_top
            and bbox[1] < run_bottom
        ]
        if len(members) < _MIN_SIDE_BLOCKS:
            return False
        covered = min(max(bbox[3] for bbox in members), run_bottom) - max(
            min(bbox[1] for bbox in members), run_top
        )
        if covered < run_length * _MIN_SIDE_COVER_FRACTION:
            return False
    return True
