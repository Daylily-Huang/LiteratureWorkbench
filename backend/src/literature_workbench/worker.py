from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from threading import Event, Lock, Thread

from literature_workbench.db.task_queue import Job, JobId, LostLease, TaskQueue
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.features.library.import_service import (
    ImportPromotionPending,
    ImportValidator,
)
from literature_workbench.features.parser.worker import ParseJobFailure, ParseWorkerService
from literature_workbench.features.translation.service import TranslationService


class JobCancellation:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: TaskQueue | None = None,
        job_id: JobId | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._loop = loop
        self._async_event = asyncio.Event()
        self._thread_event = Event()
        self._queue = queue
        self._job_id = job_id
        self._worker_id = worker_id

    @property
    def is_cancelled(self) -> bool:
        return self._thread_event.is_set()

    def cancel(self) -> None:
        if self._thread_event.is_set():
            return
        self._thread_event.set()
        self._loop.call_soon_threadsafe(self._async_event.set)

    async def wait(self) -> None:
        await self._async_event.wait()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError

    def require_current_lease(self, connection: object) -> None:
        if self._queue is None or self._job_id is None or self._worker_id is None:
            return
        self._queue.require_current_lease(
            connection,  # type: ignore[arg-type]
            self._job_id,
            self._worker_id,
        )

    def checkpoint(self, stage: str, payload: Mapping[str, object]) -> None:
        if self._queue is None or self._job_id is None or self._worker_id is None:
            raise RuntimeError("job_checkpoint_unavailable")
        self._queue.checkpoint(
            self._job_id,
            self._worker_id,
            stage,
            payload,  # type: ignore[arg-type]
        )


JobHandler = Callable[[Job, JobCancellation], Awaitable[None]]


def library_job_handlers(
    scanner: LibraryScanner, import_validator: ImportValidator | None = None
) -> Mapping[str, JobHandler]:
    async def scan_library(job: Job, cancellation: JobCancellation) -> None:
        raw_library_id = job.payload.get("library_id")
        if not isinstance(raw_library_id, str):
            raise ValueError("invalid_library_id")
        from uuid import UUID

        library_id = UUID(raw_library_id)
        cancellation.raise_if_cancelled()
        await asyncio.to_thread(scanner.scan_library, library_id)
        cancellation.raise_if_cancelled()

    async def validate_import(job: Job, cancellation: JobCancellation) -> None:
        raw_intent_id = job.payload.get("intent_id")
        if not isinstance(raw_intent_id, str) or import_validator is None:
            raise ValueError("invalid_import_intent_id")
        from uuid import UUID

        cancellation.raise_if_cancelled()
        await asyncio.to_thread(
            import_validator.validate_and_promote,
            UUID(raw_intent_id),
            cancellation,
        )
        cancellation.raise_if_cancelled()

    handlers: dict[str, JobHandler] = {"scan_library": scan_library}
    if import_validator is not None:
        handlers["validate_import"] = validate_import
    return handlers


def parser_job_handlers(parser: ParseWorkerService) -> Mapping[str, JobHandler]:
    async def parse_pdf(job: Job, cancellation: JobCancellation) -> None:
        cancellation.raise_if_cancelled()
        await asyncio.to_thread(parser.process, job, cancellation)
        cancellation.raise_if_cancelled()

    return {"parse_pdf": parse_pdf}


def translation_job_handlers(service: TranslationService) -> Mapping[str, JobHandler]:
    async def translate_selection(job: Job, cancellation: JobCancellation) -> None:
        raw_task_id = job.payload.get("task_id")
        if not isinstance(raw_task_id, str):
            raise ValueError("invalid_translation_task_id")
        from uuid import UUID

        cancellation.raise_if_cancelled()
        await asyncio.to_thread(service.process_task, UUID(raw_task_id))
        cancellation.raise_if_cancelled()

    return {"translate_selection": translate_selection}


class Worker:
    def __init__(
        self,
        queue: TaskQueue,
        worker_id: str,
        handlers: Mapping[str, JobHandler],
        *,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._queue = queue
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._lease_seconds = lease_seconds
        self._shutdown = Event()
        self._active_lock = Lock()
        self._active_cancellation: JobCancellation | None = None

    def stop(self) -> None:
        self._shutdown.set()
        with self._active_lock:
            cancellation = self._active_cancellation
        if cancellation is not None:
            cancellation.cancel()

    async def run_once(self) -> bool:
        if self._shutdown.is_set() or not self._handlers:
            return False
        job = self._queue.lease_next(
            self._worker_id,
            self._lease_seconds,
            self._handlers.keys(),
        )
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._queue.fail(job.id, self._worker_id, "unknown_job_kind", retryable=False)
            return True

        cancellation = JobCancellation(
            asyncio.get_running_loop(), self._queue, job.id, self._worker_id
        )
        with self._active_lock:
            self._active_cancellation = cancellation
        if self._shutdown.is_set():
            cancellation.cancel()
        heartbeat_stop = Event()
        heartbeat = Thread(
            target=self._heartbeat,
            args=(job, cancellation, heartbeat_stop),
            name=f"job-heartbeat-{job.id}",
            daemon=True,
        )
        heartbeat.start()
        internally_cancelled = False
        try:
            cancellation.raise_if_cancelled()
            await handler(job, cancellation)
            cancellation.raise_if_cancelled()
            self._queue.complete(job.id, self._worker_id)
        except asyncio.CancelledError:
            internally_cancelled = cancellation.is_cancelled
            cancellation.cancel()
            if not internally_cancelled:
                raise
        except ImportPromotionPending:
            try:
                self._queue.requeue_running(job.id, self._worker_id)
            except LostLease:
                cancellation.cancel()
        except ParseJobFailure as error:
            try:
                self._queue.fail(
                    job.id,
                    self._worker_id,
                    error.error_code,
                    retryable=error.retryable,
                )
            except LostLease:
                cancellation.cancel()
        except Exception:
            try:
                self._queue.fail(job.id, self._worker_id, "handler_error", retryable=True)
            except LostLease:
                cancellation.cancel()
        finally:
            heartbeat_stop.set()
            await asyncio.to_thread(heartbeat.join)
            with self._active_lock:
                if self._active_cancellation is cancellation:
                    self._active_cancellation = None
        return True

    async def run_forever(self, *, poll_interval: float = 0.25) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        try:
            while not self._shutdown.is_set():
                handled = await self.run_once()
                if not handled:
                    await asyncio.sleep(poll_interval)
        finally:
            self.stop()

    def _heartbeat(
        self,
        job: Job,
        cancellation: JobCancellation,
        stop: Event,
    ) -> None:
        interval = min(10.0, self._lease_seconds / 3)
        while not stop.wait(interval):
            if self._shutdown.is_set():
                cancellation.cancel()
                return
            try:
                self._queue.renew_lease(job.id, self._worker_id, self._lease_seconds)
            except LostLease:
                cancellation.cancel()
                return


class WorkerPool:
    def __init__(self, *workers: Worker) -> None:
        if not workers:
            raise ValueError("worker pool must not be empty")
        self._workers = workers

    def stop(self) -> None:
        for worker in self._workers:
            worker.stop()

    async def run_forever(self) -> None:
        tasks = [asyncio.create_task(worker.run_forever()) for worker in self._workers]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.stop()
            await asyncio.gather(*tasks, return_exceptions=True)
