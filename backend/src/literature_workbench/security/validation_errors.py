from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


LOGGER = logging.getLogger("literature_workbench.validation")
VALIDATION_ERROR_CODE = "request_validation_error"
_SAFE_LOCATION_ROOTS = frozenset({"body", "cookie", "header", "path", "query"})
_SAFE_SCHEMA_FIELDS = frozenset({"api_key", "default_translation_mode", "token"})
_REDACTED_LOCATION = "<redacted>"


def _sanitize_location(location: tuple[object, ...]) -> list[str | int]:
    sanitized: list[str | int] = []
    for index, part in enumerate(location):
        if isinstance(part, int):
            sanitized.append(part)
        elif index == 0 and part in _SAFE_LOCATION_ROOTS:
            sanitized.append(str(part))
        elif part in _SAFE_SCHEMA_FIELDS:
            sanitized.append(str(part))
        else:
            sanitized.append(_REDACTED_LOCATION)
    return sanitized


def install_validation_error_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        request: Request,
        exception: RequestValidationError,
    ) -> JSONResponse:
        errors = [
            {
                "loc": _sanitize_location(tuple(error.get("loc", ()))),
                "type": str(error.get("type", "value_error")),
            }
            for error in exception.errors()
        ]
        route = request.scope.get("route")
        route_name = getattr(route, "name", "unmatched_route")
        LOGGER.info("route=%s status=422 code=%s", route_name, VALIDATION_ERROR_CODE)
        return JSONResponse(
            {"code": VALIDATION_ERROR_CODE, "errors": errors},
            status_code=422,
        )
