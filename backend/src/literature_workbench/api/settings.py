from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Body, Query, Request, Response
from fastapi.responses import JSONResponse

from literature_workbench.features.egress.gateway import EgressRejected
from literature_workbench.features.egress.billing import DeepSeekBillingStatus
from literature_workbench.features.settings.models import (
    DeepSeekKeyUpdate,
    PublicSettings,
    SettingsPatch,
)
from literature_workbench.features.settings.service import SettingsService


router = APIRouter(prefix="/api/settings", tags=["settings"])


def _service(request: Request) -> SettingsService:
    return cast(SettingsService, request.app.state.settings_service)


@router.get("")
def get_settings(request: Request) -> PublicSettings:
    return _service(request).read_public()


@router.patch("")
def patch_settings(request: Request, patch: SettingsPatch) -> PublicSettings:
    return _service(request).patch_public(patch)


@router.put("/deepseek-key", status_code=204)
def put_deepseek_key(request: Request, update: DeepSeekKeyUpdate) -> Response:
    _service(request).put_deepseek_key(update.api_key.get_secret_value())
    return Response(status_code=204)


@router.delete("/deepseek-key", status_code=204)
def delete_deepseek_key(request: Request) -> Response:
    _service(request).delete_deepseek_key()
    return Response(status_code=204)


@router.post("/deepseek-probe")
def probe_deepseek(
    request: Request,
    _body: None = Body(default=None),
) -> JSONResponse:
    try:
        result = _service(request).probe_deepseek()
    except EgressRejected as error:
        return JSONResponse({"code": str(error)}, status_code=502)
    except RuntimeError:
        return JSONResponse({"code": "deepseek_probe_failed"}, status_code=502)
    return JSONResponse({"result": result})


@router.get("/deepseek-billing")
def get_deepseek_billing(
    request: Request,
    refresh: bool = Query(default=False),
) -> DeepSeekBillingStatus:
    return _service(request).read_deepseek_billing(force=refresh)
