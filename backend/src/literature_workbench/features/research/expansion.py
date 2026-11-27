"""Query expansion for cross-lingual retrieval.

Chinese questions carry no Latin tokens, so the FTS5 lexical index of an
English paper can never match them directly.  When the first-pass search
finds nothing and the question contains CJK characters, the research
service asks DeepSeek for one of two things:

- ``OVERVIEW`` — the question asks what the document is about; no keyword
  search can answer that, so the service falls back to the document's
  leading chunks (title/abstract/first page).
- A comma-separated list of English search terms that likely appear in
  relevant passages, translated into the document's terminology.

This module is pure: prompt construction and response parsing only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_MAX_TERMS = 8
_MAX_TERM_CHARS = 60
_CJK = re.compile(r"[一-鿿㐀-䶿]")
# A usable search term is built from Latin word characters plus the inner
# punctuation scientific terms actually use ("camera-trap", "Gia Lai").
_TERM_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-'. ]*[A-Za-z0-9]$|^[A-Za-z0-9]$")


@dataclass(frozen=True, slots=True)
class QueryExpansion:
    kind: Literal["overview", "keywords"]
    match_query: str | None


def contains_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def build_expansion_prompt(question: str) -> str:
    return (
        "You are helping a reader search one English academic PDF. A user "
        "asked the question below, but full-text search found nothing "
        "matching it.\n\n"
        "Reply with exactly ONE line and nothing else:\n"
        "- The single word OVERVIEW if the question asks for the document's "
        "overall topic, summary, purpose, or main idea (for example "
        '"这篇文献讲了什么", "总结一下", "what is this paper about").\n'
        "- Otherwise up to 8 English search terms or short phrases likely "
        "to appear verbatim in relevant passages, separated by commas. "
        "Translate the question's concepts into the terminology an English "
        "academic paper would use (scientific names, technical terms). Do "
        "not output whole sentences, numbering, or explanations.\n\n"
        f"<question>\n{question}\n</question>"
    )


def parse_expansion_response(text: str) -> QueryExpansion | None:
    """Parse the model's one-line reply into a retrieval directive.

    Anything beyond the first non-empty line is ignored; a reply that is
    neither OVERVIEW nor yields at least one usable term returns ``None``
    so the caller falls back to the canned no-hit answer.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("OVERVIEW"):
            return QueryExpansion(kind="overview", match_query=None)
        terms: list[str] = []
        for raw in re.split(r"[,，;；]", line):
            term = raw.strip().strip('"').strip().rstrip(".").replace('"', "")
            if not term or len(term) > _MAX_TERM_CHARS or not _TERM_OK.match(term):
                continue
            if term in terms:
                continue
            terms.append(term)
            if len(terms) >= _MAX_TERMS:
                break
        if not terms:
            return None
        match_query = " OR ".join(f'"{term}"' for term in terms)
        return QueryExpansion(kind="keywords", match_query=match_query)
    return None
