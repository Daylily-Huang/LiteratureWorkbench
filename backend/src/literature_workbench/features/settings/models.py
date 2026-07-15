from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


TranslationMode = Literal["online", "local_only"]


class PublicSettings(BaseModel):
    deepseek_key_configured: bool
    deepseek_base_url: Literal["https://api.deepseek.com"] = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    qwen_fallback_enabled: bool = False
    default_translation_mode: TranslationMode = "online"


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_translation_mode: TranslationMode | None = None
    deepseek_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    qwen_fallback_enabled: bool | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> Self:
        for field_name in (
            "default_translation_mode",
            "deepseek_model",
            "qwen_fallback_enabled",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} may be omitted but not null")
        return self


class DeepSeekKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=8, max_length=512)
