"""Context-aware academic translation prompt builder shared by DeepSeek and local Qwen."""

from __future__ import annotations

import hashlib


_MAX_TITLE_CHARS = 240
_MAX_OPENING_CHARS = 650
_MAX_CONTEXT_CHARS = 500
_MAX_GLOSSARY_CHARS = 2_000


def _clip(value: str, limit: int, *, from_end: bool = False) -> str:
    text = " ".join(value.split()).strip()
    if len(text) <= limit:
        return text
    return text[-limit:] if from_end else text[:limit]


def effective_prompt_version(base_version: str, glossary: str) -> str:
    cleaned = glossary.strip()
    if not cleaned:
        return base_version
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{base_version}:g-{digest}"


def build_academic_translation_prompt(
    *,
    selected_text: str,
    target_language: str,
    paper_title: str = "",
    paper_opening: str = "",
    context_before: str = "",
    context_after: str = "",
    glossary: str = "",
) -> str:
    """Build a single-turn academic translation prompt with server-verified context."""
    parts: list[str] = [
        f"You are an expert academic translator. Translate ONLY the text inside <selection>...</selection> into natural, accurate, publication-quality {target_language}.",
        "Rules:",
        "1. Use <paper_context> and <glossary> strictly as read-only background to identify the scientific discipline, disambiguate domain terminology, and maintain consistency. Never translate the context itself.",
        "2. Translate specialized scientific and technical terms using standard Chinese academic terminology rather than literal word-for-word rendering.",
        "3. Preserve standard abbreviations (e.g. PCR, DNA, DMSO, EDTA), author-year citations, Latin species names, and numerical units accurately.",
        "4. If the selection contains minor PDF OCR artifacts (broken hyphens, stray spaces, or obviously garbled symbols like '*/o' for '%'), silently interpret them using context without adding commentary.",
        "5. Return ONLY the translated text of <selection>. Do not include XML tags, notes, or explanations.",
    ]

    context_lines: list[str] = []
    title_clean = _clip(paper_title, _MAX_TITLE_CHARS)
    if title_clean:
        context_lines.append(f"Paper title: {title_clean}")
    opening_clean = _clip(paper_opening, _MAX_OPENING_CHARS)
    if opening_clean:
        context_lines.append(f"Paper opening/abstract excerpt: {opening_clean}")
    before_clean = _clip(context_before, _MAX_CONTEXT_CHARS, from_end=True)
    if before_clean:
        context_lines.append(f"Surrounding text before selection: {before_clean}")
    after_clean = _clip(context_after, _MAX_CONTEXT_CHARS)
    if after_clean:
        context_lines.append(f"Surrounding text after selection: {after_clean}")

    if context_lines:
        parts.append("\n<paper_context>\n" + "\n".join(context_lines) + "\n</paper_context>")

    glossary_clean = glossary.strip()[:_MAX_GLOSSARY_CHARS].strip()
    if glossary_clean:
        parts.append("\n<glossary>\n" + glossary_clean + "\n</glossary>")

    parts.append(f"\n<selection>\n{selected_text}\n</selection>")
    return "\n".join(parts)
