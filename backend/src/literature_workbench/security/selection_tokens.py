from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from literature_workbench.platform.windows.filesystem import RootIdentity


class InvalidSelectionToken(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_selection_token")


class RootSelectionTokens(Protocol):
    def issue(
        self,
        identity: RootIdentity,
        session_generation: UUID,
        ttl_seconds: int = 300,
    ) -> str: ...

    def consume(self, token: str, session_generation: UUID) -> RootIdentity: ...


@dataclass(frozen=True, slots=True)
class _Selection:
    digest: bytes
    identity: RootIdentity
    session_generation: UUID
    expires_at: datetime


class InMemoryRootSelectionTokens:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._random_bytes = random_bytes
        self._entries: dict[bytes, _Selection] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(active_count={len(self._entries)})"

    def issue(
        self,
        identity: RootIdentity,
        session_generation: UUID,
        ttl_seconds: int = 300,
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        raw = self._random_bytes(32)
        if len(raw) != 32:
            raise ValueError("random source must return exactly 32 bytes")
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        selection = _Selection(
            digest=digest,
            identity=identity,
            session_generation=session_generation,
            expires_at=self._now() + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._entries[digest] = selection
        return token

    def consume(self, token: str, session_generation: UUID) -> RootIdentity:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        with self._lock:
            matching_digest = next(
                (stored for stored in self._entries if hmac.compare_digest(stored, digest)),
                None,
            )
            if matching_digest is None:
                raise InvalidSelectionToken
            selection = self._entries[matching_digest]
            if selection.session_generation != session_generation:
                raise InvalidSelectionToken
            self._entries.pop(matching_digest)
        if self._now() >= selection.expires_at:
            raise InvalidSelectionToken
        return selection.identity

    def storage_snapshot(self) -> bytes:
        with self._lock:
            return b"".join(sorted(self._entries))
