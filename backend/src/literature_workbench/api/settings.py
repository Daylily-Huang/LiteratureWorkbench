from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response

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
