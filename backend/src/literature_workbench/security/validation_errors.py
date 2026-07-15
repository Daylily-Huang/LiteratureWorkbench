from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


LOGGER = logging.getLogger("literature_workbench.validation")
VALIDATION_ERROR_CODE = "request_validation_error"


def install_validation_error_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        request: Request,
        exception: RequestValidationError,
    ) -> JSONResponse:
        errors = [
            {
                "loc": [part for part in error.get("loc", ()) if isinstance(part, (str, int))],
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
