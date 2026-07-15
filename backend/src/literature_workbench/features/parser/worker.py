from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import Job, JsonValue
from literature_workbench.features.library.models import ContentId
from literature_workbench.features.library.fingerprint import fingerprint_verified_pdf
from literature_workbench.features.parser.classifier import (
    bounded_body_language_sample,
    classify_staged_pdf,
)
from literature_workbench.features.parser.mineru import (
    MINERU_COMMAND_VERSION,
    MinerUAdapter,
    MinerUExecutionSummary,
    MinerUFailed,
    ProcessOutcome,
    choose_ocr_language,
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
    open_current_source_for_content,
    persist_structure_validation,
    recover_checkpointed_staging,
    stage_verified_pdf,
    validate_staged_structure,
)
from literature_workbench.features.parser.workspace import (
    TrustedMinerUResult,
    TrustedParserDirectory,
    TrustedParserFile,
    TrustedParserWorkspace,
    UnsafeParserWorkspace,
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

    def require_current_lease(self, connection: object) -> None: ...

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
        data_root: Path | TrustedParserWorkspace,
        *,
        reconcile: Callable[[UUID, Path], object] | None = None,
        mineru_factory: MinerUFactory | None = None,
    ) -> None:
        self._database = database
        self._inspector = inspector
        self._workspace = (
            data_root if isinstance(data_root, TrustedParserWorkspace) else None
        )
        self._data_root = (
            data_root.data_root
            if isinstance(data_root, TrustedParserWorkspace)
            else Path(os.path.abspath(data_root))
        )
        self._reconcile = reconcile
        self._mineru_factory = mineru_factory
        self._runs = ParseRunRepository(database)

    def process(self, job: Job, cancellation: ParseCancellation) -> None:
        content_id = _content_id_from_job(job)
        request_revision = _request_revision_from_job(job)
        manual_document = job.payload.get("document_id")
        if not isinstance(manual_document, str) and not self._automatic_parse_allowed(content_id):
            return
        self._ensure_workspace()
        if self._recover_completed_publication(job, content_id, cancellation):
            return
        checkpoint_state = dict(job.checkpoint)
        source = None
        staged: StagedPdf | None = None
        run_id: UUID | None = None
        work_directory: Path | None = None
        work_capability: TrustedParserDirectory | None = None
        result_capability: TrustedMinerUResult | None = None
        promoted_result: Path | None = None
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
                    self._ensure_workspace(),
                    content_id,
                    source,
                    attempt_id,
                    _JobStagingCheckpoint(content_id, cancellation, checkpoint_state),
                    cancellation=cancellation,
                )
            validation = validate_staged_structure(staged)
            persist_structure_validation(
                self._database,
                validation,
                source,
                cancellation=cancellation,
                inspector=self._inspector,
            )
            if validation.status != "valid_pdf":
                raise ParseJobFailure("invalid_pdf_structure", retryable=False)
            staged = validation.staged
            if self._recover_promoted_attempt(
                job,
                content_id,
                source,
                staged,
                cancellation,
                checkpoint_state,
            ):
                return
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
                run_id = self._runs.create_run(
                    content_id,
                    "pymupdf",
                    parser_version,
                    cancellation=cancellation,
                    source_job_id=job.id,
                    request_revision=request_revision,
                )
                pages = extract_pymupdf_pages(staged, run_id)
            else:
                parser_version = MINERU_COMMAND_VERSION
                run_id = self._runs.create_run(
                    content_id,
                    "mineru",
                    parser_version,
                    cancellation=cancellation,
                    source_job_id=job.id,
                    request_revision=request_revision,
                )
                language = self._ocr_language(
                    job,
                    validation.title,
                    validation.authors,
                    bounded_body_language_sample(staged),
                )
                work_capability = self._ensure_workspace().create_mineru_work(run_id)
                work_directory = work_capability.path

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
                result_capability = self._ensure_workspace().seal_mineru_work(
                    content_id, run_id, work_capability
                )
                work_capability = None
                pages = validate_mineru_output(
                    staged, run_id, work_directory, parser_version
                )
                result_capability = (
                    self._ensure_workspace().promote_sealed_mineru_result(
                        result_capability
                    )
                )
                promoted_result = result_capability.path
                work_directory = None
                relative_output = Path(
                    os.path.relpath(promoted_result, self._data_root)
                ).as_posix()
                checkpoint_state.update(
                    {
                        "parse_run_id": str(run_id),
                        "mineru_output_dir": relative_output,
                        "mineru_manifest_digest": result_capability.manifest_digest,
                        "mineru_input_stem": staged.path.stem,
                    }
                )
                cancellation.checkpoint("mineru_promoted", checkpoint_state)
                self._runs.set_output_dir(
                    run_id, relative_output, cancellation=cancellation
                )
                result_capability.update_journal("output_recorded")
                cancellation.checkpoint("mineru_output_recorded", checkpoint_state)
            cancellation.raise_if_cancelled()
            self._runs.complete_run(
                run_id,
                pages,
                cancellation=cancellation,
                verify_inputs=lambda: _verify_held_inputs(
                    source, staged, result_capability
                ),
            )
            if promoted_result is not None:
                if result_capability is None:
                    raise ParseJobFailure(
                        "mineru_result_capability_missing", retryable=True
                    )
                result_capability.update_journal("db_completed")
                with self._database.transaction() as connection:
                    cancellation.require_current_lease(connection)
                    _verify_held_inputs(source, staged, result_capability)
                    self._require_publishable_result(
                        connection, job, content_id, run_id
                    )
                    if result_capability is None:
                        raise ParseJobFailure(
                            "mineru_result_capability_missing", retryable=True
                        )
                    self._ensure_workspace().publish_result_pointer(
                        content_id, run_id, result_capability
                    )
                    cancellation.require_current_lease(connection)
                    self._require_publishable_result(
                        connection, job, content_id, run_id
                    )
            checkpoint_state["completed_parse_run_id"] = str(run_id)
            cancellation.checkpoint("completed", checkpoint_state)
        except AllSourcesUnavailable as error:
            raise ParseJobFailure("all_sources_unavailable", retryable=True) from error
        except MinerUFailed as error:
            if run_id is not None and promoted_result is None:
                self._fail_run_if_running(run_id, str(error))
            raise ParseJobFailure(str(error), retryable=_mineru_error_retryable(str(error))) from error
        except UnsafeParserWorkspace as error:
            if run_id is not None:
                self._fail_run_if_running(run_id, "mineru_unsafe_result_tree")
            raise ParseJobFailure(
                "mineru_unsafe_result_tree", retryable=False
            ) from error
        except ParseJobFailure as error:
            if run_id is not None and promoted_result is None:
                self._fail_run_if_running(run_id, error.error_code)
            raise
        except asyncio.CancelledError:
            if run_id is not None and promoted_result is None:
                self._fail_run_if_running(run_id, "parse_cancelled")
            raise
        except Exception as error:
            if run_id is not None and promoted_result is None:
                self._fail_run_if_running(run_id, "parse_internal_error")
            raise ParseJobFailure("parse_internal_error", retryable=True) from error
        finally:
            if source is not None:
                source.close()
            if staged is not None:
                held_staged = staged.held_file
                if isinstance(held_staged, TrustedParserFile):
                    self._ensure_workspace().cleanup_staged(
                        held_staged,
                        content_id,
                        staged.attempt_id,
                        staged.sha256,
                    )
                elif held_staged is not None:
                    held_staged.close()
            if work_capability is not None:
                work_capability.close()
            if result_capability is not None:
                result_capability.close()

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
            self._ensure_workspace(),
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

    def _recover_promoted_attempt(
        self,
        job: Job,
        content_id: ContentId,
        source: object,
        staged: StagedPdf,
        cancellation: ParseCancellation,
        checkpoint_state: dict[str, JsonValue],
    ) -> bool:
        raw_run_id = job.checkpoint.get("parse_run_id")
        raw_digest = job.checkpoint.get("mineru_manifest_digest")
        if not isinstance(raw_run_id, str) or not isinstance(raw_digest, str):
            return False
        try:
            run_id = UUID(raw_run_id)
        except ValueError as error:
            raise ParseJobFailure(
                "mineru_recovery_checkpoint_invalid", retryable=False
            ) from error
        request_revision = _request_revision_from_job(job)
        with self._database.connect() as connection:
            run = connection.execute(
                """
                SELECT parser_name,parser_version,status,output_dir,request_revision,
                       source_job_id
                FROM parse_runs WHERE id=? AND content_sha256=?
                """,
                (str(run_id), str(content_id)),
            ).fetchone()
        if (
            run is None
            or str(run["parser_name"]) != "mineru"
            or int(run["request_revision"]) != request_revision
            or str(run["source_job_id"] or "") != str(job.id)
            or str(run["status"]) != "running"
        ):
            raise ParseJobFailure("stale_parse_publication", retryable=False)
        result: TrustedMinerUResult | None = None
        try:
            result = self._ensure_workspace().open_sealed_mineru_result(
                content_id, run_id
            )
            if result.manifest_digest != raw_digest:
                raise ParseJobFailure(
                    "mineru_recovery_manifest_mismatch", retryable=False
                )
            result.verify()
            pages = validate_mineru_output(
                staged,
                run_id,
                result.path,
                str(run["parser_version"]),
                output_stem=_mineru_input_stem_from_checkpoint(job),
            )
            expected_output = f"parse-results/{content_id}/{run_id}"
            existing_output = run["output_dir"]
            if existing_output is None:
                self._runs.set_output_dir(
                    run_id, expected_output, cancellation=cancellation
                )
            elif str(existing_output) != expected_output:
                raise ParseJobFailure(
                    "mineru_recovery_output_mismatch", retryable=False
                )
            result.update_journal("output_recorded")
            cancellation.checkpoint("mineru_output_recorded", checkpoint_state)
            self._runs.complete_run(
                run_id,
                pages,
                cancellation=cancellation,
                verify_inputs=lambda: _verify_held_inputs(
                    source, staged, result
                ),
            )
            result.update_journal("db_completed")
            with self._database.transaction() as connection:
                cancellation.require_current_lease(connection)
                _verify_held_inputs(source, staged, result)
                self._require_publishable_result(
                    connection, job, content_id, run_id
                )
                self._ensure_workspace().publish_result_pointer(
                    content_id, run_id, result
                )
                cancellation.require_current_lease(connection)
                self._require_publishable_result(
                    connection, job, content_id, run_id
                )
            checkpoint_state["completed_parse_run_id"] = str(run_id)
            cancellation.checkpoint("completed", checkpoint_state)
            return True
        finally:
            if result is not None:
                result.close()

    def _recover_completed_publication(
        self,
        job: Job,
        content_id: ContentId,
        cancellation: ParseCancellation,
    ) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT r.id,r.parser_name,r.output_dir,c.current_parse_run_id
                FROM parse_runs AS r
                JOIN contents AS c ON c.sha256=r.content_sha256
                WHERE r.source_job_id=? AND r.content_sha256=? AND r.status='completed'
                """,
                (str(job.id), str(content_id)),
            ).fetchone()
        if row is None:
            return False
        run_id = UUID(str(row["id"]))
        if str(row["current_parse_run_id"] or "") != str(run_id):
            raise ParseJobFailure("stale_parse_publication", retryable=False)
        if str(row["parser_name"]) != "mineru":
            return True
        expected_output = f"parse-results/{content_id}/{run_id}"
        if str(row["output_dir"] or "") != expected_output:
            raise ParseJobFailure("mineru_recovery_output_mismatch", retryable=True)
        result: TrustedMinerUResult | None = None
        try:
            result = self._ensure_workspace().open_sealed_mineru_result(
                content_id, run_id
            )
            with self._database.transaction() as connection:
                cancellation.require_current_lease(connection)
                self._require_publishable_result(
                    connection, job, content_id, run_id
                )
                self._ensure_workspace().publish_result_pointer(
                    content_id, run_id, result
                )
                cancellation.require_current_lease(connection)
                self._require_publishable_result(
                    connection, job, content_id, run_id
                )
        except (OSError, ValueError) as error:
            raise ParseJobFailure("mineru_recovery_failed", retryable=True) from error
        finally:
            if result is not None:
                result.close()
        return True

    @staticmethod
    def _require_publishable_result(
        connection: sqlite3.Connection,
        job: Job,
        content_id: ContentId,
        run_id: UUID,
    ) -> None:
        expected_output = f"parse-results/{content_id}/{run_id}"
        current = connection.execute(
            """
            SELECT 1
            FROM contents AS c
            JOIN parse_runs AS r ON r.id=c.current_parse_run_id
            WHERE c.sha256=? AND r.id=? AND r.source_job_id=?
              AND r.status='completed' AND r.output_dir=?
              AND r.request_revision=c.parse_request_revision
            """,
            (
                str(content_id),
                str(run_id),
                str(job.id),
                expected_output,
            ),
        ).fetchone()
        if current is None:
            raise ParseJobFailure("stale_parse_publication", retryable=False)

    def _ocr_language(
        self,
        job: Job,
        title: str | None,
        authors: tuple[str, ...],
        body_sample: str,
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
        sample = " ".join(filter(None, (body_sample, title, *authors)))
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
            lease=self._ensure_workspace().mineru_lease(timeout_seconds=3600),
        )

    def _ensure_workspace(self) -> TrustedParserWorkspace:
        if self._workspace is None:
            self._workspace = TrustedParserWorkspace.create(self._data_root)
        return self._workspace

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


def _request_revision_from_job(job: Job) -> int:
    value = job.payload.get("request_revision")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ParseJobFailure("invalid_parse_request_revision", retryable=False)
    return value


def _mineru_input_stem_from_checkpoint(job: Job) -> str:
    value = job.checkpoint.get("mineru_input_stem")
    if not isinstance(value, str):
        raise ParseJobFailure("invalid_mineru_recovery_checkpoint", retryable=False)
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ParseJobFailure(
            "invalid_mineru_recovery_checkpoint", retryable=False
        ) from error


def _mineru_error_retryable(error_code: str) -> bool:
    return error_code in {
        "mineru_cuda_oom",
        "mineru_model_missing",
        "mineru_nonzero_exit",
        "mineru_pwsh_unavailable",
    }


def _verify_held_inputs(
    source: object,
    staged: StagedPdf,
    result: TrustedMinerUResult | None = None,
) -> None:
    if not staged.verify_current_hash():
        raise ParseJobFailure("staged_identity_changed", retryable=True)
    fingerprint = fingerprint_verified_pdf(source)  # type: ignore[arg-type]
    if (
        fingerprint.sha256 != staged.content_id
        or source.current_identity() != source.identity  # type: ignore[attr-defined]
    ):
        raise ParseJobFailure("source_identity_changed", retryable=True)
    source.stream.seek(0)  # type: ignore[attr-defined]
    if result is not None:
        result.verify()
