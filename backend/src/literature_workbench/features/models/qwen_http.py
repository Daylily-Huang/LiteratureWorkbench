from __future__ import annotations

import time
from threading import Event, Lock, Thread
from types import TracebackType

import httpx


class MonotonicResponseDeadline:
    """Close a synchronous local HTTP exchange when its total deadline expires."""

    def __init__(self, client: httpx.Client, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        self._client = client
        self._deadline = time.monotonic() + timeout_seconds
        self._finished = Event()
        self._expired = Event()
        self._lock = Lock()
        self._response: httpx.Response | None = None
        self._thread: Thread | None = None

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    def __enter__(self) -> MonotonicResponseDeadline:
        self._thread = Thread(
            target=self._close_at_deadline,
            name="qwen-http-deadline",
        )
        self._thread.start()
        return self

    def attach(self, response: httpx.Response) -> None:
        with self._lock:
            self._response = response
            expired = self._expired.is_set()
        if expired:
            self._close_response(response)

    def raise_if_expired(self) -> None:
        if self.expired:
            raise httpx.TimeoutException("local response total deadline exceeded")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._finished.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        if exc_type is None:
            self.raise_if_expired()

    def _close_at_deadline(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining > 0 and self._finished.wait(remaining):
            return
        if self._finished.is_set():
            return
        self._expired.set()
        with self._lock:
            response = self._response
        if response is not None:
            self._close_response(response)
        try:
            self._client.close()
        except (OSError, RuntimeError, httpx.HTTPError):
            pass

    @staticmethod
    def _close_response(response: httpx.Response) -> None:
        try:
            response.close()
        except (OSError, RuntimeError, httpx.HTTPError):
            pass
