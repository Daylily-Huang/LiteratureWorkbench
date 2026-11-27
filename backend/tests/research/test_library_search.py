from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from literature_workbench.db.database import Database
from literature_workbench.features.research.chunker import RagChunk
from literature_workbench.features.research.repository import RagChunkRepository
from literature_workbench.features.research.retriever import RagRetriever


def _seed_document(
    database: Database,
    *,
    content_id: str,
    display_name: str,
    status: str = "valid_pdf",
    path_key: str | None = None,
) -> str:
    """Seed one registered document (and its contents row) for retrieval."""
    now = datetime.now(UTC).isoformat()
    document_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO libraries VALUES(?,?,?,?,?,1,'monitored',1,'test','active',?,?)",
            ("seed-library", "/library", "/library", "vol", "root", now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO contents(
                 sha256,size_bytes,mime_type,page_count,status,created_at,updated_at)
               VALUES(?,12,'application/pdf',1,'valid_pdf',?,?)""",
            (content_id, now, now),
        )
        connection.execute(
            """INSERT INTO documents(
                 id,library_id,content_sha256,path,path_key,display_name,format,size_bytes,
                 mtime_ns,volume_id,file_id,hard_link_count,parse_preference,
                 auto_cloud_eligible,authorization_source,status,created_at,updated_at)
                 VALUES(?,'seed-library',?,?,?,?,'pdf',12,1,'vol','file',1,'parse',1,
                        'registered_library',?,?,?)""",
            (
                document_id,
                content_id,
                f"/library/{document_id}.pdf",
                path_key or f"/library/{document_id}.pdf",
                display_name,
                status,
                now,
                now,
            ),
        )
    return document_id


def _index(
    repository: RagChunkRepository,
    content_id: str,
    texts: list[str],
) -> None:
    repository.rebuild(
        content_id,
        str(uuid4()),
        [RagChunk(index + 1, index, text) for index, text in enumerate(texts)],
    )


def test_library_search_attributes_hits_to_documents(research_db: Database) -> None:
    first = _seed_document(
        research_db, content_id="1" * 64, display_name="chevrotain.pdf"
    )
    second = _seed_document(
        research_db, content_id="2" * 64, display_name="conifer.pdf"
    )
    repository = RagChunkRepository(research_db)
    _index(repository, "1" * 64, ["Tragulus versicolor camera trap evidence"])
    _index(repository, "2" * 64, ["subalpine coniferous forest dynamics"])

    hits = RagRetriever(repository).search_library("camera trap evidence")

    assert len(hits) == 1
    assert hits[0].document_id == first
    assert hits[0].display_name == "chevrotain.pdf"
    assert second != hits[0].document_id


def test_library_search_caps_fragments_per_document(research_db: Database) -> None:
    _seed_document(research_db, content_id="3" * 64, display_name="long.pdf")
    _seed_document(research_db, content_id="4" * 64, display_name="short.pdf")
    repository = RagChunkRepository(research_db)
    _index(repository, "3" * 64, [f"chevrotain paragraph {n}" for n in range(5)])
    _index(repository, "4" * 64, ["chevrotain sidebar note"])

    hits = RagRetriever(repository).search_library("chevrotain")

    per_document: dict[str, int] = {}
    for hit in hits:
        per_document[hit.document_id] = per_document.get(hit.document_id, 0) + 1
    assert max(per_document.values()) <= 3
    # The smaller document still appears alongside the dominant one.
    assert len(per_document) == 2


def test_library_search_dedupes_chunks_across_hard_linked_documents(
    research_db: Database,
) -> None:
    _seed_document(research_db, content_id="5" * 64, display_name="first-name.pdf")
    _seed_document(
        research_db,
        content_id="5" * 64,
        display_name="second-name.pdf",
        path_key="/library/second-name.pdf",
    )
    repository = RagChunkRepository(research_db)
    _index(repository, "5" * 64, ["chevrotain shared content"])

    hits = RagRetriever(repository).search_library("chevrotain")

    assert len(hits) == 1


def test_library_search_skips_documents_not_valid(research_db: Database) -> None:
    _seed_document(
        research_db, content_id="6" * 64, display_name="gone.pdf", status="missing"
    )
    repository = RagChunkRepository(research_db)
    _index(repository, "6" * 64, ["chevrotain orphan chunks"])

    assert RagRetriever(repository).search_library("chevrotain") == []


def test_library_search_includes_candidate_documents(research_db: Database) -> None:
    # Scanner-registered documents sit in candidate_pdf until confirmed; they
    # are still readable library members, so library search must cover them.
    _seed_document(
        research_db,
        content_id="8" * 64,
        display_name="candidate.pdf",
        status="candidate_pdf",
    )
    repository = RagChunkRepository(research_db)
    _index(repository, "8" * 64, ["chevrotain candidate evidence"])

    hits = RagRetriever(repository).search_library("chevrotain")

    assert len(hits) == 1
    assert hits[0].display_name == "candidate.pdf"


def test_library_sweep_indexes_candidate_documents(research_db: Database) -> None:
    from conftest import make_page, publish_pages

    publish_pages(research_db, "9" * 64, [make_page(1, ["candidate sweep text"])])
    _seed_document(
        research_db,
        content_id="9" * 64,
        display_name="candidate.pdf",
        status="candidate_pdf",
    )
    repository = RagChunkRepository(research_db)

    candidates = repository.parsed_documents()

    assert [content for _doc, content, _run in candidates] == ["9" * 64]


def test_library_search_respects_limit_and_tokenless_questions(
    research_db: Database,
) -> None:
    _seed_document(research_db, content_id="7" * 64, display_name="paper.pdf")
    repository = RagChunkRepository(research_db)
    _index(repository, "7" * 64, [f"chevrotain paragraph {n}" for n in range(6)])
    retriever = RagRetriever(repository)

    assert len(retriever.search_library("chevrotain", limit=4, per_document=6)) == 4
    assert retriever.search_library("？？？") == []
