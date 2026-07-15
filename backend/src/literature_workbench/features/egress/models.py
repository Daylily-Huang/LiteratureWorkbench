from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class VerifiedSelectionProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    document_id: UUID
    document_version_id: UUID
    content_sha256: str = Field(min_length=1, max_length=128)
    physical_page: int = Field(ge=1)
    verified_text: str = Field(min_length=1, max_length=65_536)
    target_language: Literal["zh-CN"]
    model: str = Field(min_length=1, max_length=128)
