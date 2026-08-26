from __future__ import annotations

from conftest import make_page, publish_pages

from literature_workbench.db.database import Database
from literature_workbench.features.research.chunker import RagChunk, chunk_pages
from literature_workbench.features.research.repository import RagChunkRepository

CONTENT = "a" * 64


def _chunks(*texts: str) -> list[RagChunk]:
    return [RagChunk(1, index, text) for index, text in enumerate(texts)]


def test_read_pages_returns_reading_order_bound_to_parse_run(research_db: Database) -> None:
    pages = [make_page(1, ["first page"]), make_page(2, ["second page"])]
    run_id = publish_pages(research_db, CONTENT, pages)
    repository = RagChunkRepository(research_db)
    current = repository.read_pages(CONTENT, run_id)
    assert [page.physical_page for page in current] == [1, 2]
    assert current[0].blocks[0].text == "first page"
    assert current[1].normalized_text == "second page"

    # Reading an unassociated parse_run returns empty
    assert repository.read_pages(CONTENT, "non-existent-run") == []


def test_rebuild_replaces_previous_chunks_atomically(research_db: Database) -> None:
    publish_pages(research_db, CONTENT, [make_page(1, ["old"])])
    repository = RagChunkRepository(research_db)
    repository.rebuild(CONTENT, "run-a", _chunks("one", "two"))
    repository.rebuild(CONTENT, "run-b", _chunks("only"))
    rows = repository.search(CONTENT, '"one" OR "only" OR "two"', limit=10)
    assert [(row["chunk_id"], row["text"]) for row in rows] != []
    texts = {row["text"] for row in rows}
    assert texts == {"only"}


def test_indexed_run_tracks_staleness(research_db: Database) -> None:
    run_id = publish_pages(research_db, CONTENT, [make_page(1, ["text"])])
    repository = RagChunkRepository(research_db)
    assert repository.indexed_parse_run_id(CONTENT) is None
    repository.rebuild(CONTENT, run_id, _chunks("text"))
    assert repository.indexed_parse_run_id(CONTENT) == run_id
    # A newer parse makes the index stale.
    new_run_id = publish_pages(research_db, CONTENT, [make_page(1, ["text"])])
    assert repository.indexed_parse_run_id(CONTENT) != new_run_id


def test_search_filters_by_content(research_db: Database) -> None:
    other_content = "b" * 64
    publish_pages(research_db, CONTENT, [make_page(1, ["shared term alpha"])])
    publish_pages(research_db, other_content, [make_page(1, ["shared term alpha"])])
    repository = RagChunkRepository(research_db)
    repository.rebuild(CONTENT, "run-a", _chunks("shared term alpha"))
    repository.rebuild(other_content, "run-b", _chunks("shared term alpha"))
    rows = repository.search(CONTENT, '"alpha"', limit=10)
    assert len(rows) == 1
    assert rows[0]["chunk_id"] is not None


def test_chunk_pages_integration_with_published_pages(research_db: Database) -> None:
    run_id = publish_pages(research_db, CONTENT, [make_page(1, ["Annamites " * 200]), make_page(2, ["short"])])
    repository = RagChunkRepository(research_db)
    pages = repository.read_pages(CONTENT, run_id)
    chunks = chunk_pages(pages, max_chars=1000)
    assert all(chunk.physical_page in (1, 2) for chunk in chunks)
    assert any("Annamites" in chunk.text for chunk in chunks)


def test_search_and_overview_return_rects_json(research_db: Database) -> None:
    repository = RagChunkRepository(research_db)
    repository.rebuild(
        CONTENT,
        "run-a",
        [
            RagChunk(1, 0, "first chunk about chevrotains", ((10, 20, 300, 80),)),
            RagChunk(1, 1, "second chunk about chevrotains", ((10, 90, 300, 150),)),
        ],
    )
    search_rows = repository.search(CONTENT, '"chevrotains"', limit=10)
    assert len(search_rows) == 2
    assert all(row["rects_json"] is not None for row in search_rows)

    overview_rows = repository.overview_chunks(CONTENT, limit=1)
    assert len(overview_rows) == 1
    assert overview_rows[0]["text"] == "first chunk about chevrotains"
    assert overview_rows[0]["rects_json"] == "[[10, 20, 300, 80]]"
