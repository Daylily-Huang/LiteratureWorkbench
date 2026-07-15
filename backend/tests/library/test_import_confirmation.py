from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from literature_workbench.api.app import create_app
from literature_workbench.config import AppSettings
from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.features.library.import_intents import ImportIntentRepository
from literature_workbench.features.library.import_service import ImportService
from literature_workbench.security.local_session import LocalSession


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database.open(tmp_path / "unit.sqlite3")
    run_migrations(value)
    return value


def _headers(session: LocalSession, token: str | None = None) -> dict[str, str]:
    headers = {
        "Origin": "http://testserver",
        "X-LW-CSRF": session.csrf_token,
    }
    if token is not None:
        headers["X-LW-Upload-Token"] = token
        headers["Content-Type"] = "application/pdf"
    return headers


def test_composed_api_requires_confirmation_then_accepts_raw_pdf(tmp_path: Path) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    body = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"
    with TestClient(
        app,
        base_url="http://testserver",
        cookies={"lw_session": session.session_cookie},
    ) as client:
        created = client.post(
            "/api/imports/intents",
            headers=_headers(session),
            json={
                "original_name": "paper.pdf",
                "size": len(body),
                "media_type": "application/pdf",
            },
        )
        intent = created.json()
        accepted = client.post(
            f"/api/imports/{intent['intent_id']}/content",
            headers={**_headers(session, intent["upload_token"]), "Content-Length": str(len(body))},
            content=body,
        )
        polled = client.get(f"/api/imports/{intent['intent_id']}")
        inventory = client.get("/api/imports?active=true")

    assert created.status_code == 201
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "uploaded"
    assert polled.json()["status"] == "uploaded"
    assert [item["intent_id"] for item in inventory.json()["items"]] == [intent["intent_id"]]
    assert (tmp_path / "data" / "imports" / ".incoming" / f"{intent['intent_id']}.uploaded.pdf").exists()


@pytest.mark.asyncio
async def test_token_is_consumed_before_body_and_replay_reads_zero_bytes(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, lambda: datetime.now(UTC))
    created = repository.create(
        "generation-a", "paper.pdf", len(b"%PDF-1.7"), "application/pdf"
    )
    service = ImportService(
        database,
        repository,
        tmp_path,
        session_generation="generation-a",
        now=lambda: datetime.now(UTC),
    )
    first_reads = 0
    replay_reads = 0

    async def first() -> AsyncIterator[bytes]:
        nonlocal first_reads
        first_reads += 1
        yield b"%PDF-1.7"

    async def replay() -> AsyncIterator[bytes]:
        nonlocal replay_reads
        replay_reads += 1
        yield b"%PDF-1.7"

    result = await service.consume(created.intent_id, created.upload_token, first())
    with pytest.raises(KeyError, match="import_not_found"):
        await service.consume(created.intent_id, created.upload_token, replay())

    assert result.status == "uploaded"
    assert first_reads == 1
    assert replay_reads == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status, upload_token_hash FROM import_intents WHERE id = ?",
            (str(created.intent_id),),
        ).fetchone()
    assert tuple(row) == ("uploaded", None)


def test_wrong_session_and_expired_tokens_are_indistinguishable_and_read_no_body(
    tmp_path: Path, database
) -> None:
    current = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    repository = ImportIntentRepository(database, lambda: current)
    wrong = repository.create("other-generation", "paper.pdf", 9, "application/pdf")
    expired = repository.create("generation-a", "paper.pdf", 9, "application/pdf")
    current += timedelta(minutes=16)
    service = ImportService(
        database,
        repository,
        tmp_path,
        session_generation="generation-a",
        now=lambda: current,
    )

    async def chunks() -> AsyncIterator[bytes]:
        raise AssertionError("body was read")
        yield b""

    async def consume_both() -> None:
        for candidate in (wrong, expired):
            with pytest.raises(KeyError, match="import_not_found"):
                await service.consume(candidate.intent_id, candidate.upload_token, chunks())

    import asyncio

    asyncio.run(consume_both())
    assert list((tmp_path / "imports" / ".incoming").glob("*")) == [] if (tmp_path / "imports" / ".incoming").exists() else True
    with database.connect() as connection:
        expired_row = connection.execute(
            "SELECT status,upload_token_hash FROM import_intents WHERE id=?",
            (str(expired.intent_id),),
        ).fetchone()
    assert tuple(expired_row) == ("failed", None)


def test_expiry_sweep_atomically_nulls_pending_token_digest(tmp_path: Path, database) -> None:
    current = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)

    def now() -> datetime:
        return current

    repository = ImportIntentRepository(database, now)
    created = repository.create("generation-a", "paper.pdf", 9, "application/pdf")
    current += timedelta(minutes=16)

    assert repository.expire_pending() == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status,upload_token_hash,error_code FROM import_intents WHERE id=?",
            (str(created.intent_id),),
        ).fetchone()
    assert tuple(row) == ("failed", None, "intent_expired")


def test_raw_content_endpoint_rejects_multipart_before_spooling(tmp_path: Path) -> None:
    session = LocalSession()
    app = create_app(
        AppSettings(data_root=tmp_path / "data", allowed_hosts=("testserver",)),
        local_session=session,
        register_initial_library=False,
    )
    with TestClient(app, base_url="http://testserver", cookies={"lw_session": session.session_cookie}) as client:
        created = client.post(
            "/api/imports/intents",
            headers=_headers(session),
            json={"original_name": "paper.pdf", "size": 9, "media_type": "application/pdf"},
        ).json()
        response = client.post(
            f"/api/imports/{created['intent_id']}/content",
            headers={**_headers(session, created["upload_token"]), "Content-Type": "multipart/form-data; boundary=x", "Content-Length": "9"},
            content=b"%PDF-1.7",
        )

    assert response.status_code == 415
    assert not (tmp_path / "data" / "imports" / ".incoming").exists()


def test_declared_content_length_mismatch_consumes_token_without_reading_body(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, lambda: datetime.now(UTC))
    created = repository.create("generation-a", "paper.pdf", 9, "application/pdf")
    service = ImportService(database, repository, tmp_path, "generation-a")
    reads = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal reads
        reads += 1
        yield b"%PDF-1.7"

    with pytest.raises(ValueError, match="content_length_mismatch"):
        import asyncio

        asyncio.run(
            service.consume(
                created.intent_id,
                created.upload_token,
                chunks(),
                declared_length=8,
            )
        )

    assert reads == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status, upload_token_hash FROM import_intents WHERE id=?",
            (str(created.intent_id),),
        ).fetchone()
    assert tuple(row) == ("failed", None)


def test_concurrent_upload_claims_have_one_writer_and_one_zero_read_loser(
    tmp_path: Path, database
) -> None:
    repository = ImportIntentRepository(database, lambda: datetime.now(UTC))
    body = b"%PDF-1.7"
    created = repository.create("generation-a", "paper.pdf", len(body), "application/pdf")
    services = [
        ImportService(database, repository, tmp_path, "generation-a"),
        ImportService(database, repository, tmp_path, "generation-a"),
    ]
    reads = [0, 0]

    async def chunks(index: int) -> AsyncIterator[bytes]:
        reads[index] += 1
        yield body

    async def race() -> list[object]:
        return await __import__("asyncio").gather(
            *(service.consume(created.intent_id, created.upload_token, chunks(index)) for index, service in enumerate(services)),
            return_exceptions=True,
        )

    results = __import__("asyncio").run(race())

    assert sum(isinstance(result, KeyError) for result in results) == 1
    assert sorted(reads) == [0, 1]
    assert len(list((tmp_path / "imports" / ".incoming").glob("*.uploaded.pdf"))) == 1
