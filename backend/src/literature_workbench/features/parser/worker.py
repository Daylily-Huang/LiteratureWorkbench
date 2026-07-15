from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import Job, JsonValue
from literature_workbench.features.library.models import ContentId
from literature_workbench.features.parser.classifier import classify_staged_pdf
from literature_workbench.features.parser.mineru import (
    MINERU_COMMAND_VERSION,
    MinerUAdapter,
    MinerUExecutionSummary,
    MinerUFailed,
    ProcessOutcome,
    choose_ocr_language,
    promote_mineru_result,
    validate_mineru_output,
)
from literature_workbench.features.parser.models import StagedPdf
from literature_workbench.features.parser.normalizer import (
    ParseRunRepository,
    extract_pymupdf_pages,
)
from literature_workbench.features.parser.staging import (
    AllSourcesUnavailable,
    StagingCheckpointSink,
    cleanup_checkpointed_staging,
    open_current_source_for_content,
    persist_structure_validation,
    recover_checkpointed_staging,
    stage_verified_pdf,
    validate_staged_structure,
)
from literature_workbench.platform.windows.filesystem import SafeFileInspector
from literature_workbench.platform.windows.job_object import (
    ProcessIdentity,
    ProcessSpec,
    launch_suspended_in_job,
)


class ParseJobFailure(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


class ParseCancellation(Protocol):
    @property
    def is_cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...

    def checkpoint(self, stage: str, payload: Mapping[str, JsonValue]) -> None: ...


class MinerURunner(Protocol):
    def run(
        self,
        staged: StagedPdf,
        attempt_output_dir: Path,
        ocr_language: Literal["en", "ch"],
    ) -> ProcessOutcome: ...


MinerUFactory = Callable[[Callable[[ProcessIdentity], None]], MinerURunner]


class _JobStagingCheckpoint(StagingCheckpointSink):
    def __init__(
        self,
        content_id: ContentId,
        cancellation: ParseCancellation,
        state: dict[str, JsonValue],
    ) -> None:
        self._content_id = content_id
        self._cancellation = cancellation
        self._state = state

    def record_copy_started(self, attempt_id: UUID) -> None:
        self._state.update(
            {
                "attempt_id": str(attempt_id),
                "staging_relative_path": (
                    f"staging/{self._content_id}/{attempt_id}.part"
                ),
                "staging_sha256": None,
            }
        )
        self._cancellation.checkpoint("staging_copy_started", self._state)

    def record_staged(self, relative_path: str, sha256: str) -> None:
        self._state.update(
            {"staging_relative_path": relative_path, "staging_sha256": sha256}
        )
        self._cancellation.checkpoint("staged", self._state)


class ParseWorkerService:
    def __init__(
        self,
        database: Database,
        inspector: SafeFileInspector,
        data_root: Path,
        *,
        reconcile: Callable[[UUID, Path], object] | None = None,
        mineru_factory: MinerUFactory | None = None,
    ) -> None:
        self._database = database
        self._inspector = inspector
        self._data_root = Path(data_root)
        self._reconcile = reconcile
        self._mineru_factory = mineru_factory
        self._runs = ParseRunRepository(database)

    def process(self, job: Job, cancellation: ParseCancellation) -> None:
        content_id = _content_id_from_job(job)
        manual_document = job.payload.get("document_id")
        if not isinstance(manual_document, str) and not self._automatic_parse_allowed(content_id):
            return
        checkpoint_state = dict(job.checkpoint)
        source = None
        staged: StagedPdf | None = None
        run_id: UUID | None = None
        work_directory: Path | None = None
        try:
            cancellation.raise_if_cancelled()
            source = open_current_source_for_content(
                self._database,
                self._inspector,
                content_id,
                reconcile=self._reconcile,
            )
            staged = self._recover(job, content_id)
            if staged is None:
                attempt_id = uuid4()
                staged = stage_verified_pdf(
                    self._data_root,
                    content_id,
                    source,
                    attempt_id,
                    _JobStagingCheckpoint(content_id, cancellation, checkpoint_state),
                )
            validation = validate_staged_structure(staged)
            persist_structure_validation(self._database, validation, source.identity)
            if validation.status != "valid_pdf":
                raise ParseJobFailure("invalid_pdf_structure", retryable=False)
            staged = validation.staged
            checkpoint_state.update(
                {"page_count": staged.page_count, "structure_status": "valid_pdf"}
            )
            cancellation.checkpoint("classified", checkpoint_state)
            requested_strategy = str(job.payload.get("strategy") or "auto")
            if requested_strategy not in {"auto", "pymupdf", "mineru"}:
                raise ParseJobFailure("invalid_parse_strategy", retryable=False)
            strategy = (
                classify_staged_pdf(staged)
                if requested_strategy == "auto"
                else requested_strategy
            )
            cancellation.raise_if_cancelled()
            if strategy == "pymupdf":
                parser_version = str(fitz.VersionBind)
                run_id = self._runs.create_run(content_id, "pymupdf", parser_version)
                pages = extract_pymupdf_pages(staged, run_id)
            else:
                parser_version = MINERU_COMMAND_VERSION
                run_id = self._runs.create_run(content_id, "mineru", parser_version)
                language = self._ocr_language(job, validation.title, validation.authors)
                work_directory = self._data_root / "mineru-work" / str(run_id)

                def record_identity(identity: ProcessIdentity) -> None:
                    checkpoint_state["process"] = {
                        "attempt_id": str(identity.attempt_id),
                        "pid": identity.pid,
                        "created_ns": identity.created_ns,
                        "executable": Path(identity.executable).name,
                        "arguments_sha256": hashlib.sha256(
                            "\0".join(identity.arguments).encode("utf-8")
                        ).hexdigest(),
                        "job_token": identity.job_token,
                    }
                    cancellation.checkpoint("mineru_running", checkpoint_state)

                def audit(summary: MinerUExecutionSummary) -> None:
                    checkpoint_state["mineru_summary"] = {
                        "command_version": summary.command_version,
                        "attempt_id": str(summary.attempt_id),
                        "started_at": summary.started_at,
                        "finished_at": summary.finished_at,
                        "exit_code": summary.exit_code,
                        "error_code": summary.error_code,
                    }
                    cancellation.checkpoint("mineru_finished", checkpoint_state)

                runner = self._mineru_runner(record_identity, audit, cancellation)
                runner.run(staged, work_directory, language)
                pages = validate_mineru_output(
                    staged, run_id, work_directory, parser_version
                )
                promoted = promote_mineru_result(
                    self._data_root, content_id, run_id, work_directory
                )
                work_directory = None
                relative_output = Path(os.path.relpath(promoted, self._data_root)).as_posix()
                self._runs.set_output_dir(run_id, relative_output)
            cancellation.raise_if_cancelled()
            self._runs.complete_run(run_id, pages)
            checkpoint_state["completed_parse_run_id"] = str(run_id)
            cancellation.checkpoint("completed", checkpoint_state)
        except AllSourcesUnavailable as error:
            raise ParseJobFailure("all_sources_unavailable", retryable=True) from error
        except MinerUFailed as error:
            if run_id is not None:
                self._fail_run_if_running(run_id, str(error))
            raise ParseJobFailure(str(error), retryable=_mineru_error_retryable(str(error))) from error
        except ParseJobFailure as error:
            if run_id is not None:
                self._fail_run_if_running(run_id, error.error_code)
            raise
        except asyncio.CancelledError:
            if run_id is not None:
                self._fail_run_if_running(run_id, "parse_cancelled")
            raise
        except Exception as error:
            if run_id is not None:
                self._fail_run_if_running(run_id, "parse_internal_error")
            raise ParseJobFailure("parse_internal_error", retryable=True) from error
        finally:
            if source is not None:
                source.close()
            if staged is not None:
                cleanup_checkpointed_staging(
                    self._data_root,
                    content_id,
                    staged.attempt_id,
                    f"staging/{content_id}/{staged.attempt_id}.pdf",
                )
                cleanup_checkpointed_staging(
                    self._data_root,
                    content_id,
                    staged.attempt_id,
                    f"staging/{content_id}/{staged.attempt_id}.part",
                )
            if work_directory is not None and work_directory.exists():
                shutil.rmtree(work_directory)

    def _recover(self, job: Job, content_id: ContentId) -> StagedPdf | None:
        raw_attempt = job.checkpoint.get("attempt_id")
        relative = job.checkpoint.get("staging_relative_path")
        expected_hash = job.checkpoint.get("staging_sha256")
        if not isinstance(raw_attempt, str) or not isinstance(relative, str):
            return None
        try:
            attempt_id = UUID(raw_attempt)
        except ValueError:
            return None
        return recover_checkpointed_staging(
            self._data_root,
            content_id,
            attempt_id,
            relative,
            ContentId(expected_hash if isinstance(expected_hash, str) else str(content_id)),
        )

    def _automatic_parse_allowed(self, content_id: ContentId) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM documents
                WHERE content_sha256=? AND status <> 'missing' AND parse_preference='parse'
                LIMIT 1
                """,
                (content_id,),
            ).fetchone()
        return row is not None

    def _ocr_language(
        self,
        job: Job,
        title: str | None,
        authors: tuple[str, ...],
    ) -> Literal["en", "ch"]:
        requested = job.payload.get("ocr_language")
        if requested is not None:
            if requested not in {"en", "ch"}:
                raise ParseJobFailure("invalid_ocr_language", retryable=False)
            return cast(Literal["en", "ch"], requested)
        document_id = job.payload.get("document_id")
        override = None
        if isinstance(document_id, str):
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT ocr_language_override FROM documents WHERE id=?",
                    (document_id,),
                ).fetchone()
            if row is not None and row["ocr_language_override"] in {"en", "ch"}:
                override = str(row["ocr_language_override"])
        sample = " ".join(filter(None, (title, *authors)))
        return choose_ocr_language(sample, override)  # type: ignore[arg-type]

    def _mineru_runner(
        self,
        record_identity: Callable[[ProcessIdentity], None],
        audit: Callable[[MinerUExecutionSummary], None],
        cancellation: ParseCancellation,
    ) -> MinerURunner:
        if self._mineru_factory is not None:
            return self._mineru_factory(record_identity)

        def launch(
            spec: ProcessSpec,
            attempt_id: UUID,
            record: Callable[[ProcessIdentity], None],
        ) -> ProcessOutcome:
            process = launch_suspended_in_job(spec, attempt_id, record)
            exit_code, stdout, stderr = process.wait_and_collect(
                cancellation_check=cancellation.raise_if_cancelled
            )
            return ProcessOutcome(exit_code, stdout, stderr)

        return MinerUAdapter(
            launch=launch,
            record_identity=record_identity,
            audit=audit,
        )

    def _fail_run_if_running(self, run_id: UUID, error_code: str) -> None:
        try:
            self._runs.fail_run(run_id, error_code)
        except ValueError as error:
            if str(error) != "parse_run_not_running":
                raise


def _content_id_from_job(job: Job) -> ContentId:
    value = job.payload.get("content_id")
    if not isinstance(value, str):
        value = job.payload.get("content_sha256")
    if not isinstance(value, str):
        raise ParseJobFailure("invalid_content_id", retryable=False)
    return ContentId(value)


def _mineru_error_retryable(error_code: str) -> bool:
    return error_code in {
        "mineru_cuda_oom",
        "mineru_model_missing",
        "mineru_nonzero_exit",
        "mineru_pwsh_unavailable",
    }
