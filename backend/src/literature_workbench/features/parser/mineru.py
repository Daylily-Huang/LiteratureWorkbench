from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]
from pydantic import ValidationError

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.models import ParsedBlock, ParsedPage, StagedPdf
from literature_workbench.platform.windows.job_object import (
    ProcessIdentity,
    ProcessSpec,
    launch_suspended_in_job,
)


DEFAULT_MINERU_SCRIPT = Path(r"D:\MinerU\run_mineru.ps1")
MINERU_COMMAND_VERSION = "mineru-pwsh-v1"
_GLOBAL_MINERU_LEASE = threading.BoundedSemaphore(1)


class MinerUFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class MinerUExecutionSummary:
    command_version: str
    attempt_id: UUID
    started_at: str
    finished_at: str
    exit_code: int
    error_code: str | None


LaunchMinerU = Callable[
    [ProcessSpec, UUID, Callable[[ProcessIdentity], None]],
    ProcessOutcome,
]


def build_mineru_spec(
    staged: StagedPdf,
    attempt_output_dir: Path,
    ocr_language: Literal["en", "ch"],
    *,
    which: Callable[[str], str | None] = shutil.which,
    script_path: Path = DEFAULT_MINERU_SCRIPT,
) -> ProcessSpec:
    if ocr_language not in {"en", "ch"}:
        raise MinerUFailed("mineru_invalid_ocr_language")
    resolved_pwsh = which("pwsh.exe")
    if resolved_pwsh is None or Path(resolved_pwsh).name.casefold() != "pwsh.exe":
        raise MinerUFailed("mineru_pwsh_unavailable")
    output = Path(attempt_output_dir)
    output.mkdir(parents=True, exist_ok=True)
    args = (
        resolved_pwsh,
        "-NoProfile",
        "-File",
        str(script_path),
        "-InputPath",
        str(staged.path),
        "-OutputPath",
        str(output),
        "-Backend",
        "pipeline",
        "-m",
        "auto",
        "-l",
        ocr_language,
    )
    return ProcessSpec(
        args=args,
        command_line=subprocess.list2cmdline(list(args)),
        cwd=output,
        shell=False,
    )


def choose_ocr_language(
    sample_text: str,
    override: Literal["en", "ch"] | None,
) -> Literal["en", "ch"]:
    if override is not None:
        return override
    han = sum("\u3400" <= character <= "\u9fff" for character in sample_text)
    latin = sum(
        ("A" <= character <= "Z") or ("a" <= character <= "z")
        for character in sample_text
    )
    return "ch" if han > latin else "en"


class MinerUAdapter:
    def __init__(
        self,
        *,
        launch: LaunchMinerU | None = None,
        which: Callable[[str], str | None] = shutil.which,
        audit: Callable[[MinerUExecutionSummary], None] | None = None,
        record_identity: Callable[[ProcessIdentity], None] | None = None,
        lease: threading.BoundedSemaphore = _GLOBAL_MINERU_LEASE,
    ) -> None:
        self._launch = launch or _launch_process
        self._which = which
        self._audit = audit or (lambda _: None)
        self._record_identity = record_identity or (lambda _: None)
        self._lease = lease

    def run(
        self,
        staged: StagedPdf,
        attempt_output_dir: Path,
        ocr_language: Literal["en", "ch"],
    ) -> ProcessOutcome:
        attempt_id = uuid4()
        started = datetime.now(UTC)
        exit_code = -1
        error_code: str | None = None
        with self._lease:
            try:
                spec = build_mineru_spec(
                    staged,
                    attempt_output_dir,
                    ocr_language,
                    which=self._which,
                )
                outcome = self._launch(spec, attempt_id, self._record_identity)
                exit_code = outcome.exit_code
                if exit_code != 0:
                    error_code = _fixed_process_error(
                        exit_code, outcome.stdout, outcome.stderr
                    )
                    raise MinerUFailed(error_code)
                return outcome
            except MinerUFailed as error:
                error_code = str(error)
                raise
            finally:
                self._audit(
                    MinerUExecutionSummary(
                        command_version=MINERU_COMMAND_VERSION,
                        attempt_id=attempt_id,
                        started_at=started.isoformat(),
                        finished_at=datetime.now(UTC).isoformat(),
                        exit_code=exit_code,
                        error_code=error_code,
                    )
                )


def validate_mineru_output(
    staged: StagedPdf,
    parse_run_id: UUID,
    attempt_output_dir: Path,
    parser_version: str,
) -> list[ParsedPage]:
    if staged.page_count is None or staged.page_count <= 0:
        raise MinerUFailed("mineru_invalid_staged_page_count")
    result_directory = Path(attempt_output_dir) / staged.path.stem / "auto"
    content_list_path = result_directory / f"{staged.path.stem}_content_list.json"
    if not content_list_path.is_file():
        raise MinerUFailed("mineru_output_missing")
    try:
        raw_blocks = json.loads(content_list_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MinerUFailed("mineru_invalid_output") from error
    if not isinstance(raw_blocks, list):
        raise MinerUFailed("mineru_invalid_output")
    metadata_path = result_directory / f"{staged.path.stem}_metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise MinerUFailed("mineru_invalid_output") from error
        if (
            not isinstance(metadata, dict)
            or metadata.get("page_count") != staged.page_count
        ):
            raise MinerUFailed("mineru_page_count_mismatch")
    per_page: list[list[ParsedBlock]] = [[] for _ in range(staged.page_count)]
    try:
        for ordinal, raw in enumerate(raw_blocks):
            if not isinstance(raw, dict):
                raise ValueError("block_not_object")
            page_index = raw.get("page_idx")
            bbox = raw.get("bbox")
            if (
                not isinstance(page_index, int)
                or isinstance(page_index, bool)
                or not 0 <= page_index < staged.page_count
                or not isinstance(bbox, list)
                or len(bbox) != 4
            ):
                raise ValueError("block_page_or_bbox_invalid")
            text_value = raw.get("text")
            text = text_value if isinstance(text_value, str) else None
            confidence_value = raw.get("score")
            confidence = (
                float(confidence_value)
                if isinstance(confidence_value, (int, float))
                and not isinstance(confidence_value, bool)
                else None
            )
            block = ParsedBlock(
                block_id=f"{page_index}:{ordinal}",
                kind=_normalized_block_kind(raw.get("type")),
                text=text,
                bbox_1000=tuple(float(item) for item in bbox),  # type: ignore[arg-type]
                source="mineru_ocr",
                confidence=confidence,
            )
            per_page[page_index].append(block)
    except (TypeError, ValueError, ValidationError) as error:
        raise MinerUFailed("mineru_invalid_output") from error
    pages: list[ParsedPage] = []
    try:
        with fitz.open(staged.path) as document:
            if document.page_count != staged.page_count:
                raise MinerUFailed("mineru_page_count_mismatch")
            for page_index, blocks in enumerate(per_page):
                page = document.load_page(page_index)
                raw_text = "\n".join(block.text for block in blocks if block.text is not None)
                normalized_text = " ".join(raw_text.split())
                pages.append(
                    ParsedPage(
                        content_id=staged.content_id,
                        page_index=page_index,
                        physical_page=page_index + 1,
                        parse_run_id=parse_run_id,
                        parser_name="mineru",
                        parser_version=parser_version,
                        normalized_text=normalized_text,
                        raw_text=raw_text,
                        width_points=page.rect.width,
                        height_points=page.rect.height,
                        coordinate_reliable=True,
                        blocks=blocks,
                    )
                )
    except MinerUFailed:
        raise
    except Exception as error:
        raise MinerUFailed("mineru_invalid_output") from error
    return pages


def promote_mineru_result(
    data_root: Path,
    content_id: ContentId,
    attempt_id: UUID,
    attempt_output_dir: Path,
) -> Path:
    root = Path(data_root) / "parse-results" / str(content_id)
    root.mkdir(parents=True, exist_ok=True)
    final = root / str(attempt_id)
    if final.exists():
        raise FileExistsError(final)
    os.rename(attempt_output_dir, final)
    relative = f"parse-results/{content_id}/{attempt_id}"
    pointer = {
        "attempt_id": str(attempt_id),
        "attempt_path": relative,
        "path_digest": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
    }
    temporary = root / f"current.{attempt_id}.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(pointer, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / "current.json")
    return final


def _fixed_process_error(exit_code: int, stdout: bytes, stderr: bytes) -> str:
    combined = (stdout + b"\n" + stderr).lower()
    signatures = (
        (b"cuda out of memory", "mineru_cuda_oom"),
        (b"model not found", "mineru_model_missing"),
        (b"invalid input", "mineru_invalid_input"),
    )
    for signature, code in signatures:
        if signature in combined:
            return code
    return "mineru_nonzero_exit" if exit_code != 0 else "mineru_invalid_output"


def _normalized_block_kind(
    raw: object,
) -> Literal["text", "title", "formula", "image", "table", "caption"]:
    value = str(raw or "text").casefold()
    if value in {"title", "image", "table"}:
        return value  # type: ignore[return-value]
    if value in {"equation", "formula", "interline_equation"}:
        return "formula"
    if "caption" in value:
        return "caption"
    return "text"


def _launch_process(
    spec: ProcessSpec,
    attempt_id: UUID,
    record_identity: Callable[[ProcessIdentity], None],
) -> ProcessOutcome:
    process = launch_suspended_in_job(spec, attempt_id, record_identity)
    exit_code, stdout, stderr = process.wait_and_collect()
    return ProcessOutcome(exit_code, stdout, stderr)
