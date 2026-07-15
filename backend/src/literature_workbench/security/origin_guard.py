from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OriginGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self._has_forwarding_header(request):
            return self._reject()

        if request.url.path == "/api/session/bootstrap" and request.url.query:
            return JSONResponse({"code": "not_found"}, status_code=404)

        origin = request.headers.get("Origin")
        expected_origin = f"http://{request.headers.get('Host', '')}"
        if request.method in _WRITE_METHODS:
            if origin is None or origin != expected_origin:
                return self._reject()
        elif request.method in _SAFE_METHODS and origin is not None and origin != expected_origin:
            return self._reject()

        return await call_next(request)

    @staticmethod
    def _has_forwarding_header(request: Request) -> bool:
        for raw_name, _ in request.scope.get("headers", []):
            name = raw_name.decode("latin-1").lower()
            if name == "forwarded" or name.startswith("x-forwarded-"):
                return True
        return False

    @staticmethod
    def _reject() -> JSONResponse:
        return JSONResponse({"code": "origin_rejected"}, status_code=403)
