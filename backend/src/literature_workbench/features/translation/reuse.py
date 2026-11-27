"""Exact source matching only: never infer an alignment inside an old translation."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranslationPiece:
    source: str
    translation: str | None = None


def word_boundary(text: str, start: int, end: int) -> bool:
    # Avoid matching "significant" inside "insignificant" or partial numbers.
    return not (
        start > 0 and text[start - 1].isalnum() and text[start].isalnum()
        or end < len(text) and text[end - 1].isalnum() and text[end].isalnum()
    )


def sentence_end(text: str) -> bool:
    tail = text.rstrip(" \t\r\n\"'”’)]")
    # Dotted initials and common scientific abbreviations are ambiguous joins.
    # Prefer translating the whole sentence rather than guessing alignment.
    if re.search(r"(?:\b[A-Za-z]\.){2,}$", tail) or re.search(
        r"(?:\b(?:Mr|Mrs|Dr|Prof|Fig|Figs|Eq|Eqs|al|vs|etc|sp|spp|cf|approx|"
        r"incl|excl|vol|pp|ref|refs|ed|eds|sec|ch)|\b[A-Za-z]|\d)\.$",
        tail, re.IGNORECASE,
    ):
        return False
    return bool(re.search(r"[.!?。！？]$", tail))


def reuse_plan(
    text: str, memories: list[TranslationPiece], *, prefer_exact: bool = False,
) -> tuple[str, list[TranslationPiece]]:
    for memory in memories:
        if memory.source == text:
            return "exact", [memory]
    if not prefer_exact:
        covering = [m for m in memories if text in m.source and word_boundary(
            m.source, m.source.index(text), m.source.index(text) + len(text)
        )]
        if covering:
            return "reference", [min(covering, key=lambda item: len(item.source))]

    # Reuse only complete sentence/paragraph joins. A changed negation or a
    # wider selection within one sentence must be translated as a whole.
    matches: list[tuple[int, int, TranslationPiece]] = []
    for memory in memories:
        offset = 0
        while (start := text.find(memory.source, offset)) >= 0:
            end = start + len(memory.source)
            if word_boundary(text, start, end) and (start == 0 or sentence_end(text[:start])) and (
                end == len(text) or sentence_end(memory.source)
            ):
                matches.append((start, end, memory))
            offset = end
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    pieces: list[TranslationPiece] = []
    cursor = 0
    for start, end, memory in matches:
        if start < cursor:
            continue
        if text[cursor:start].strip():
            pieces.append(TranslationPiece(text[cursor:start].strip()))
        pieces.append(memory)
        cursor = end
    if text[cursor:].strip():
        pieces.append(TranslationPiece(text[cursor:].strip()))
    if not matches or sum(p.translation is None for p in pieces) > 3:
        return "fresh", [TranslationPiece(text)]
    return ("partial" if any(p.translation is None for p in pieces) else "exact"), pieces
