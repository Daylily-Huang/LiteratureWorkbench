from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


TranslationMode = Literal["online", "local_only"]


class PublicSettings(BaseModel):
    deepseek_key_configured: bool
    deepseek_base_url: Literal["https://api.deepseek.com"] = "https://api.deepseek.com"
    primary_fast_model: str = "deepseek-v4-flash"
    primary_strong_model: str = "deepseek-v4-pro"
    default_translation_mode: TranslationMode = "online"


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_translation_mode: TranslationMode | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> Self:
        if (
            "default_translation_mode" in self.model_fields_set
            and self.default_translation_mode is None
        ):
            raise ValueError("default_translation_mode may be omitted but not null")
        return self


class DeepSeekKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=8, max_length=512)
