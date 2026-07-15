from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class ApiRoutePolicy(Enum):
    PUBLIC_READ = "public_read"
    PUBLIC_BOOTSTRAP = "public_bootstrap"
    SESSION_READ = "session_read"
    SESSION_WRITE = "session_write"
    UNCLASSIFIED = "unclassified"


def api_route_policy(path: str, method: str) -> ApiRoutePolicy:
    normalized_method = method.upper()
    if path == "/api/health" and normalized_method == "GET":
        return ApiRoutePolicy.PUBLIC_READ
    if path == "/api/session/bootstrap" and normalized_method == "POST":
        return ApiRoutePolicy.PUBLIC_BOOTSTRAP
    if path.startswith("/api/"):
        if normalized_method in {"GET", "HEAD", "OPTIONS"}:
            return ApiRoutePolicy.SESSION_READ
        if normalized_method in {"POST", "PUT", "PATCH", "DELETE"}:
            return ApiRoutePolicy.SESSION_WRITE
    return ApiRoutePolicy.UNCLASSIFIED


@dataclass(frozen=True, slots=True)
class _BootstrapToken:
    digest: bytes
    expires_at: float


class LocalSession:
    __slots__ = (
        "_bootstrap_tokens",
        "_lock",
        "_now",
        "csrf_token",
        "session_cookie",
        "session_generation",
    )

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self.session_cookie = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.session_generation = str(uuid.uuid4())
        self._now = now
        self._lock = threading.Lock()
        self._bootstrap_tokens: dict[bytes, _BootstrapToken] = {}

    def issue_bootstrap_token(self, ttl_seconds: float = 60) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).digest()
        entry = _BootstrapToken(digest=digest, expires_at=self._now() + ttl_seconds)
        with self._lock:
            self._bootstrap_tokens[digest] = entry
        return token

    def consume_bootstrap_token(self, token: str) -> bool:
        candidate = hashlib.sha256(token.encode("utf-8")).digest()
        with self._lock:
            matched_key = next(
                (
                    digest
                    for digest in self._bootstrap_tokens
                    if hmac.compare_digest(digest, candidate)
                ),
                None,
            )
            if matched_key is None:
                return False
            entry = self._bootstrap_tokens.pop(matched_key)
            return self._now() < entry.expires_at

    def valid_session(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate, self.session_cookie)

    def valid_csrf(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate, self.csrf_token)


class LocalApiSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, session: LocalSession) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._session = session

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        policy = api_route_policy(request.url.path, request.method)
        if policy in {ApiRoutePolicy.SESSION_READ, ApiRoutePolicy.SESSION_WRITE}:
            if not self._session.valid_session(request.cookies.get("lw_session")):
                return JSONResponse({"code": "session_required"}, status_code=403)
        if policy is ApiRoutePolicy.SESSION_WRITE:
            if not self._session.valid_csrf(request.headers.get("X-LW-CSRF")):
                return JSONResponse({"code": "csrf_required"}, status_code=403)
        return await call_next(request)
