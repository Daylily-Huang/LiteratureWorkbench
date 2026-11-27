from __future__ import annotations

from conftest import make_page, publish_pages

from literature_workbench.db.database import Database
from literature_workbench.features.research.chunker import RagChunk
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.retriever import (
    RagRetriever,
    build_match_query,
    rects_from_json,
)

CONTENT = "a" * 64


def test_build_match_query_quotes_and_dedupes_tokens() -> None:
    query = build_match_query('Where was the "silver-backed" chevrotain found? found?')
    assert query == '"Where" OR "was" OR "the" OR "silver-backed" OR "chevrotain" OR "found"'


def test_build_match_query_drops_short_latin_tokens() -> None:
    # Trigram tokenization cannot match tokens shorter than 3 characters.
    assert build_match_query("is it an ox?") is None
    assert build_match_query("is it an owl?") == '"owl"'


def test_build_match_query_slices_cjk_runs_into_windows() -> None:
    query = build_match_query("银背鼷鹿在哪里？")
    assert query == '"银背鼷鹿" OR "鼷鹿在哪" OR "鹿在哪里"'


def test_build_match_query_keeps_short_cjk_run_whole() -> None:
    assert build_match_query("鼷鹿是什么动物") == '"鼷鹿是什" OR "是什么动" OR "什么动物"'


def test_build_match_query_rejects_punctuation_and_empty() -> None:
    assert build_match_query("!?.,;") is None
    assert build_match_query("") is None
    # Two-character runs fall below the trigram minimum.
    assert build_match_query("在哪") is None


def test_rects_from_json_defends_against_malformed_data() -> None:
    assert rects_from_json(None) == ()
    assert rects_from_json("") == ()
    assert rects_from_json("invalid json string") == ()
    assert rects_from_json("{}") == ()
    assert rects_from_json("[1, 2, 3]") == ()
    assert rects_from_json("[[1, 2, 3]]") == ()
    assert rects_from_json('[[10, 20, "abc", 50]]') == ()
    assert rects_from_json("[[10, 20, NaN, 50]]") == ()
    # Inverted box coordinates x1 <= x0 or y1 <= y0 are dropped
    assert rects_from_json("[[50, 50, 10, 10]]") == ()
    # Negative or out of bounds (> 1000) coordinates are dropped
    assert rects_from_json("[[-5, 20, 300, 80]]") == ()
    assert rects_from_json("[[10, 20, 1050, 80]]") == ()
    assert rects_from_json("[[10, 20, 300, 1200]]") == ()
    assert rects_from_json("[[10, 20, Infinity, 80]]") == ()
    # Valid box is parsed accurately
    assert rects_from_json("[[10, 20, 300, 80]]") == ((10, 20, 300, 80),)


def test_search_returns_best_page_first(research_db: Database) -> None:
    publish_pages(research_db, CONTENT, [make_page(1, ["irrelevant"]), make_page(3, ["Gia Lai specimen"])])
    repository = RagChunkRepository(research_db)
    repository.rebuild(
        CONTENT,
        "run-a",
        [
            RagChunk(1, 0, "The survey covered several provinces in Vietnam."),
            RagChunk(3, 1, "The specimen was recorded in Gia Lai province."),
        ],
    )
    hits = RagRetriever(repository).search(CONTENT, "Which province held the Gia Lai specimen?")
    assert hits[0].physical_page == 3
    assert "Gia Lai" in hits[0].text


def test_search_matches_chinese_corpus_via_trigram(research_db: Database) -> None:
    repository = RagChunkRepository(research_db)
    repository.rebuild(
        CONTENT,
        "run-a",
        [
            RagChunk(1, 0, "川西王朗亚高山暗针叶林动态监测样地物种组成与群落结构特征"),
            RagChunk(2, 1, "unrelated English paragraph entirely"),
        ],
    )
    hits = RagRetriever(repository).search(CONTENT, "暗针叶林的物种组成")
    assert len(hits) == 1
    assert hits[0].physical_page == 1


def test_search_returns_rects_with_hits(research_db: Database) -> None:
    repository = RagChunkRepository(research_db)
    repository.rebuild(
        CONTENT,
        "run-a",
        [RagChunk(1, 0, "chevrotain paragraph", ((10, 20, 300, 80), (10, 90, 300, 150)))],
    )
    hits = RagRetriever(repository).search(CONTENT, "chevrotain")
    assert len(hits) == 1
    assert hits[0].rects == ((10, 20, 300, 80), (10, 90, 300, 150))


def test_search_ranks_multiple_hits(research_db: Database) -> None:
    repository = RagChunkRepository(research_db)
    repository.rebuild(
        CONTENT,
        "run-a",
        [
            RagChunk(1, 0, "snaring"),
            RagChunk(2, 1, "snaring snaring snaring widespread empty forest syndrome"),
            RagChunk(3, 2, "unrelated content entirely"),
        ],
    )
    hits = RagRetriever(repository).search(CONTENT, "snaring")
    assert len(hits) == 2
    assert all("snaring" in hit.text for hit in hits)


def test_search_without_tokens_returns_nothing(research_db: Database) -> None:
    repository = RagChunkRepository(research_db)
    repository.rebuild(CONTENT, "run-a", [RagChunk(1, 0, "anything")])
    assert RagRetriever(repository).search(CONTENT, "？？？……") == []


def test_search_respects_limit(research_db: Database) -> None:
    repository = RagChunkRepository(research_db)
    repository.rebuild(
        CONTENT,
        "run-a",
        [RagChunk(index + 1, index, f"chevrotain paragraph {index}") for index in range(10)],
    )
    hits = RagRetriever(repository).search(CONTENT, "chevrotain", limit=3)
    assert len(hits) == 3
