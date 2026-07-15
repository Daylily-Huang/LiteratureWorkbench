from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from literature_workbench.features.library.models import ContentId


ParseStrategy: TypeAlias = Literal["pymupdf", "mineru"]
RequestedParseStrategy: TypeAlias = Literal["auto", "pymupdf", "mineru"]


@dataclass(frozen=True, slots=True)
class StagedPdf:
    content_id: ContentId
    attempt_id: UUID
    path: Path
    sha256: str
    page_count: int | None


class ParsedBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str
    kind: Literal["text", "title", "formula", "image", "table", "caption"]
    text: str | None
    bbox_1000: tuple[float, float, float, float]
    source: Literal["pymupdf", "mineru_ocr"]
    confidence: float | None

    @field_validator("bbox_1000")
    @classmethod
    def validate_bbox(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        if not all(math.isfinite(item) and 0 <= item <= 1000 for item in value):
            raise ValueError("bbox_coordinates_out_of_range")
        x0, y0, x1, y1 = value
        if x0 > x1 or y0 > y1:
            raise ValueError("bbox_coordinates_reversed")
        return value

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError("confidence_out_of_range")
        return value


class ParsedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id: ContentId
    page_index: int
    physical_page: int
    parse_run_id: UUID
    parser_name: Literal["pymupdf", "mineru"]
    parser_version: str
    normalized_text: str
    raw_text: str
    width_points: float
    height_points: float
    coordinate_reliable: bool
    blocks: list[ParsedBlock]

    @field_validator("page_index")
    @classmethod
    def validate_page_index(cls, value: int) -> int:
        if value < 0:
            raise ValueError("page_index_must_be_nonnegative")
        return value

    @field_validator("width_points", "height_points")
    @classmethod
    def validate_dimension(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("page_dimension_must_be_finite_and_positive")
        return value

    @model_validator(mode="after")
    def validate_physical_page(self) -> ParsedPage:
        if self.physical_page != self.page_index + 1:
            raise ValueError("physical_page_mismatch")
        return self
