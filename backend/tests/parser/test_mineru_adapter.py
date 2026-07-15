from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from uuid import uuid4

import fitz
import pytest

from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.mineru import (
    MinerUAdapter,
    MinerUExecutionSummary,
    MinerUFailed,
    ProcessOutcome,
    build_mineru_spec,
    choose_ocr_language,
    promote_mineru_result,
    validate_mineru_output,
)
from literature_workbench.features.parser.models import StagedPdf
from literature_workbench.platform.windows.job_object import ProcessSpec


def _staged_pdf(tmp_path: Path, pages: int = 3) -> StagedPdf:
    path = tmp_path / "staging" / ("a" * 64) / "owned.pdf"
    path.parent.mkdir(parents=True)
    document = fitz.open()
    try:
        for _ in range(pages):
            document.new_page(width=400, height=600)
        document.save(path)
    finally:
        document.close()
    return StagedPdf(ContentId("a" * 64), uuid4(), path, "a" * 64, pages)


def _write_content_list(output: Path, stem: str, blocks: list[dict[str, object]]) -> None:
    result = output / stem / "auto"
    result.mkdir(parents=True)
    (result / f"{stem}_content_list.json").write_text(
        json.dumps(blocks, ensure_ascii=False),
        encoding="utf-8",
    )


def test_exact_pwsh_argument_array_has_no_shell_or_source_path(tmp_path: Path) -> None:
    staged = _staged_pdf(tmp_path)
    output = tmp_path / "owned-output"
    pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"

    spec = build_mineru_spec(
        staged,
        output,
        "ch",
        which=lambda executable: pwsh if executable == "pwsh.exe" else None,
    )

    assert spec.args == (
        pwsh,
        "-NoProfile",
        "-File",
        r"D:\MinerU\run_mineru.ps1",
        "-InputPath",
        str(staged.path),
        "-OutputPath",
        str(output),
        "-Backend",
        "pipeline",
        "-m",
        "auto",
        "-l",
        "ch",
    )
    assert spec.shell is False
    assert spec.cwd == output
    assert "original-library" not in spec.command_line


@pytest.mark.parametrize("resolved", [None, r"C:\Windows\System32\powershell.exe"])
def test_pwsh_discovery_rejects_missing_or_windows_powershell(
    tmp_path: Path,
    resolved: str | None,
) -> None:
    with pytest.raises(MinerUFailed, match="mineru_pwsh_unavailable"):
        build_mineru_spec(
            _staged_pdf(tmp_path),
            tmp_path / "output",
            "en",
            which=lambda _: resolved,
        )


def test_content_list_normalizes_blocks_and_keeps_trailing_blank_pages(
    tmp_path: Path,
) -> None:
    staged = _staged_pdf(tmp_path, pages=3)
    output = tmp_path / "mineru-output"
    _write_content_list(
        output,
        staged.path.stem,
        [
            {"page_idx": 0, "type": "title", "text": " Heading ", "bbox": [0, 0, 500, 80]},
            {"page_idx": 1, "type": "text", "text": "OCR body", "bbox": [10, 20, 990, 200], "score": 0.8},
        ],
    )

    pages = validate_mineru_output(staged, uuid4(), output, "mineru-test")

    assert [page.physical_page for page in pages] == [1, 2, 3]
    assert pages[0].normalized_text == "Heading"
    assert pages[1].blocks[0].confidence == 0.8
    assert pages[2].normalized_text == ""
    assert pages[2].blocks == []
    assert all(page.width_points == 400 for page in pages)


@pytest.mark.parametrize(
    "blocks",
    [
        [{"page_idx": 3, "type": "text", "text": "bad", "bbox": [0, 0, 1, 1]}],
        [{"page_idx": 0, "type": "text", "text": "bad", "bbox": [-1, 0, 1, 1]}],
        [{"page_idx": 0, "type": "text", "text": "bad", "bbox": [10, 0, 1, 1]}],
    ],
)
def test_page_and_bbox_validation_fail_closed(
    tmp_path: Path,
    blocks: list[dict[str, object]],
) -> None:
    staged = _staged_pdf(tmp_path, pages=3)
    output = tmp_path / "invalid-output"
    _write_content_list(output, staged.path.stem, blocks)

    with pytest.raises(MinerUFailed, match="mineru_invalid_output"):
        validate_mineru_output(staged, uuid4(), output, "mineru-test")


def test_zero_exit_without_required_json_is_not_success(tmp_path: Path) -> None:
    staged = _staged_pdf(tmp_path)

    with pytest.raises(MinerUFailed, match="mineru_output_missing"):
        validate_mineru_output(staged, uuid4(), tmp_path / "empty-output", "version")


def test_independent_page_count_metadata_must_match(tmp_path: Path) -> None:
    staged = _staged_pdf(tmp_path, pages=3)
    output = tmp_path / "metadata-output"
    _write_content_list(output, staged.path.stem, [])
    result = output / staged.path.stem / "auto"
    (result / f"{staged.path.stem}_metadata.json").write_text(
        '{"page_count":2}', encoding="utf-8"
    )

    with pytest.raises(MinerUFailed, match="mineru_page_count_mismatch"):
        validate_mineru_output(staged, uuid4(), output, "version")


def test_language_choice_defaults_image_only_to_en_and_honors_override() -> None:
    assert choose_ocr_language("", None) == "en"
    assert choose_ocr_language("Latin metadata title", None) == "en"
    assert choose_ocr_language("中文标题与摘要", None) == "ch"
    assert choose_ocr_language("", "ch") == "ch"


def test_adapter_serializes_jobs_and_never_persists_stream_text(tmp_path: Path) -> None:
    staged = _staged_pdf(tmp_path, pages=1)
    sentinel = "SENTINEL private OCR paragraph"
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    release = threading.Event()
    summaries: list[MinerUExecutionSummary] = []

    def launch(
        spec: ProcessSpec,
        attempt_id: object,
        record: object,
    ) -> ProcessOutcome:
        nonlocal active, maximum_active
        assert spec.shell is False
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        release.wait(timeout=2)
        with lock:
            active -= 1
        return ProcessOutcome(1, sentinel.encode(), sentinel.encode())

    adapter = MinerUAdapter(
        launch=launch,
        which=lambda _: r"C:\PowerShell\pwsh.exe",
        audit=summaries.append,
        lease=threading.BoundedSemaphore(1),
    )
    errors: list[str] = []

    def run() -> None:
        try:
            adapter.run(staged, tmp_path / f"output-{threading.get_ident()}", "en")
        except MinerUFailed as error:
            errors.append(str(error))

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert maximum_active == 1
    assert errors == ["mineru_nonzero_exit", "mineru_nonzero_exit"]
    assert len(summaries) == 2
    assert sentinel not in repr(summaries)


def test_result_promotion_keeps_immutable_attempts_and_switches_only_pointer(
    tmp_path: Path,
) -> None:
    staged = _staged_pdf(tmp_path, pages=1)
    first = uuid4()
    second = uuid4()
    first_work = tmp_path / "work-first"
    second_work = tmp_path / "work-second"
    first_work.mkdir()
    second_work.mkdir()
    (first_work / "result.json").write_text("first", encoding="utf-8")
    (second_work / "result.json").write_text("second", encoding="utf-8")

    first_final = promote_mineru_result(tmp_path, staged.content_id, first, first_work)
    second_final = promote_mineru_result(tmp_path, staged.content_id, second, second_work)

    pointer = json.loads(
        (tmp_path / "parse-results" / staged.content_id / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert (first_final / "result.json").read_text(encoding="utf-8") == "first"
    assert (second_final / "result.json").read_text(encoding="utf-8") == "second"
    assert pointer["attempt_id"] == str(second)
    assert "path_digest" in pointer
