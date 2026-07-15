from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import sleep

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import JobId, LostLease, TaskQueue
from literature_workbench.worker import JobCancellation, Worker
from literature_workbench.features.library.import_service import ImportPromotionPending

from conftest import FakeClock


def test_identical_active_dedup_key_returns_one_job_id(queue: TaskQueue) -> None:
    first = queue.enqueue("parse", "parse:sha256-a", {"content_sha256": "sha256-a"})
    second = queue.enqueue("parse", "parse:sha256-a", {"content_sha256": "sha256-a"})

    assert second == first
    assert queue.count() == 1


def test_completed_job_allows_same_dedup_key_to_be_enqueued_again(queue: TaskQueue) -> None:
    first = queue.enqueue("parse", "parse:sha256-a", {"content_sha256": "sha256-a"})
    leased = queue.lease_next("worker-a", lease_seconds=30)
    assert leased is not None
    queue.complete(leased.id, "worker-a")

    second = queue.enqueue("parse", "parse:sha256-a", {"content_sha256": "sha256-a"})

    assert second != first
    assert queue.count(kind="parse", status="completed") == 1
    assert queue.count(kind="parse", status="queued") == 1


def test_expired_lease_is_recovered(queue: TaskQueue, clock: FakeClock) -> None:
    job_id = queue.enqueue("parse", "parse:sha256-a", {"content_sha256": "sha256-a"})
    leased = queue.lease_next("worker-a", lease_seconds=30)
    assert leased is not None and leased.id == job_id

    clock.advance(seconds=31)
    assert queue.requeue_expired() == 1
    recovered = queue.lease_next("worker-b", lease_seconds=30)
    assert recovered is not None and recovered.id == job_id
    assert recovered.attempts == 2


def test_completed_jobs_cannot_be_leased_again(queue: TaskQueue) -> None:
    queue.enqueue("parse", "parse:a", {"value": [1, True, None]})
    leased = queue.lease_next("worker-a", lease_seconds=30)
    assert leased is not None
    assert leased.payload == {"value": [1, True, None]}
    queue.complete(leased.id, "worker-a")

    assert queue.lease_next("worker-b", lease_seconds=30) is None


def test_stale_owner_cannot_checkpoint_or_complete_released_job(
    queue: TaskQueue, clock: FakeClock
) -> None:
    job_id = queue.enqueue("parse", "parse:a", {})
    assert queue.lease_next("worker-a", lease_seconds=30) is not None
    clock.advance(seconds=31)
    assert queue.requeue_expired() == 1
    assert queue.lease_next("worker-b", lease_seconds=30) is not None

    with pytest.raises(LostLease):
        queue.checkpoint(job_id, "worker-a", "promoting", {"path": "final.pdf"})
    with pytest.raises(LostLease):
        queue.complete(job_id, "worker-a")


def test_renew_and_mutations_require_the_current_running_owner(
    queue: TaskQueue, clock: FakeClock
) -> None:
    job_id = queue.enqueue("translate", "translation:a", {"text": "hello"})
    leased = queue.lease_next("worker-a", lease_seconds=30)
    assert leased is not None

    expiry = queue.renew_lease(job_id, "worker-a", lease_seconds=60)
    assert expiry == clock.now() + timedelta(seconds=60)
    queue.checkpoint(job_id, "worker-a", "sent", {"request_id": "request-a"})

    with pytest.raises(LostLease):
        queue.fail(job_id, "worker-b", "provider_error", retryable=True)

    queue.fail(job_id, "worker-a", "provider_error", retryable=False)
    assert queue.count(status="failed") == 1


def test_enqueue_in_transaction_uses_callers_transaction(database: Database) -> None:
    queue = TaskQueue(database, lambda: datetime.now(UTC))

    with pytest.raises(RuntimeError), database.transaction() as connection:
        job_id = queue.enqueue_in_transaction(connection, "scan", "scan:a", {"library_id": "a"})
        assert isinstance(job_id, str)
        raise RuntimeError("roll back metadata and job together")

    assert queue.count() == 0


def test_two_queue_instances_lease_one_job_only_once(
    database: Database, fake_clock: FakeClock
) -> None:
    producer = TaskQueue(database, fake_clock.now)
    producer.enqueue("parse", "parse:a", {})
    barrier = Barrier(3)

    def lease(worker_id: str) -> JobId | None:
        worker_queue = TaskQueue(database, fake_clock.now)
        barrier.wait()
        job = worker_queue.lease_next(worker_id, lease_seconds=30)
        return None if job is None else job.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(lease, "worker-a"), executor.submit(lease, "worker-b")]
        barrier.wait()
        results = [future.result(timeout=5) for future in futures]

    assert sum(result is not None for result in results) == 1
    assert producer.count(status="running") == 1
    assert producer.count(status="queued") == 0


@pytest.mark.asyncio
async def test_worker_with_empty_registry_leases_nothing(database: Database) -> None:
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    queue.enqueue("parse_pdf", "parse:a", {})
    worker = Worker(queue, "worker-a", {}, lease_seconds=1)

    assert await worker.run_once() is False
    assert queue.count(status="queued") == 1


@pytest.mark.asyncio
async def test_worker_heartbeat_renews_during_blocking_thread_work(database: Database) -> None:
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    other_queue = TaskQueue(database, lambda: datetime.now(UTC))
    queue.enqueue("parse_pdf", "parse:a", {})
    blocking_started = Event()

    async def handler(_job: object, cancellation: JobCancellation) -> None:
        cancellation.raise_if_cancelled()

        def blocking_parse() -> None:
            blocking_started.set()
            sleep(1.5)

        await asyncio.to_thread(blocking_parse)
        cancellation.raise_if_cancelled()

    worker = Worker(queue, "worker-a", {"parse_pdf": handler}, lease_seconds=1)
    running = asyncio.create_task(worker.run_once())
    assert await asyncio.to_thread(blocking_started.wait, 1.0)

    await asyncio.sleep(1.1)
    assert other_queue.requeue_expired() == 0
    assert other_queue.lease_next("worker-b", lease_seconds=1) is None

    assert await running is True
    assert queue.count(status="completed") == 1


@pytest.mark.asyncio
async def test_lost_heartbeat_cancels_side_effect_after_blocking_return(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    queue.enqueue("translate_selection", "translate:a", {})
    heartbeat_attempted = Event()
    sentinel_sends: list[str] = []

    def lose_lease(*_args: object, **_kwargs: object) -> datetime:
        heartbeat_attempted.set()
        raise LostLease("lease was reassigned")

    monkeypatch.setattr(queue, "renew_lease", lose_lease)

    async def handler(_job: object, cancellation: JobCancellation) -> None:
        await asyncio.to_thread(sleep, 0.5)
        cancellation.raise_if_cancelled()
        sentinel_sends.append("cloud-send")

    worker = Worker(
        queue,
        "worker-a",
        {"translate_selection": handler},
        lease_seconds=1,
    )

    assert await worker.run_once() is True
    assert heartbeat_attempted.is_set()
    assert sentinel_sends == []
    assert queue.count(status="running") == 1


@pytest.mark.asyncio
async def test_shutdown_between_lease_and_cancellation_registration_blocks_side_effect(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    queue.enqueue("validate_import", "import:a", {})
    sentinel_promotions: list[str] = []

    async def handler(_job: object, cancellation: JobCancellation) -> None:
        cancellation.raise_if_cancelled()
        sentinel_promotions.append("promoted")

    worker = Worker(queue, "worker-a", {"validate_import": handler}, lease_seconds=1)
    original_lease_next = queue.lease_next

    def lease_then_stop(worker_id: str, lease_seconds: int) -> object:
        job = original_lease_next(worker_id, lease_seconds)
        worker.stop()
        return job

    monkeypatch.setattr(queue, "lease_next", lease_then_stop)

    assert await worker.run_once() is True
    assert sentinel_promotions == []


@pytest.mark.asyncio
async def test_promotion_waiter_is_requeued_instead_of_failed(database: Database) -> None:
    queue = TaskQueue(database, lambda: datetime.now(UTC))
    job_id = queue.enqueue("validate_import", "import:waiter", {})

    async def handler(_job: object, _cancellation: JobCancellation) -> None:
        raise ImportPromotionPending("promotion_owner_pending")

    worker = Worker(queue, "worker-a", {"validate_import": handler}, lease_seconds=30)

    assert await worker.run_once() is True
    assert queue.count(status="queued") == 1
    assert queue.count(status="failed") == 0
    leased_again = queue.lease_next("worker-b", lease_seconds=30)
    assert leased_again is not None and leased_again.id == job_id
