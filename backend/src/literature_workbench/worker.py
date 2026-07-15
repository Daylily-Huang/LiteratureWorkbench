from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from threading import Event, Lock, Thread

from literature_workbench.db.task_queue import Job, LostLease, TaskQueue
from literature_workbench.features.library.scanner import LibraryScanner
from literature_workbench.features.library.import_service import ImportValidator


class JobCancellation:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._async_event = asyncio.Event()
        self._thread_event = Event()

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
        await asyncio.to_thread(import_validator.validate_and_promote, UUID(raw_intent_id))
        cancellation.raise_if_cancelled()

    handlers: dict[str, JobHandler] = {"scan_library": scan_library}
    if import_validator is not None:
        handlers["validate_import"] = validate_import
    return handlers


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
        job = self._queue.lease_next(self._worker_id, self._lease_seconds)
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._queue.fail(job.id, self._worker_id, "unknown_job_kind", retryable=False)
            return True

        cancellation = JobCancellation(asyncio.get_running_loop())
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
