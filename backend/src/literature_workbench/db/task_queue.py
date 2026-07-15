from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, NewType, TypeAlias, cast
from uuid import uuid4

from literature_workbench.db.database import Database


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JobId = NewType("JobId", str)
JobStatus: TypeAlias = Literal["queued", "running", "completed", "failed"]


class LostLease(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Job:
    id: JobId
    kind: str
    dedup_key: str
    payload: Mapping[str, JsonValue]
    status: JobStatus
    stage: str
    checkpoint: Mapping[str, JsonValue]
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None


class TaskQueue:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self._database = database
        self._now = now

    def enqueue(
        self,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, JsonValue],
        stage: str = "pending",
    ) -> JobId:
        with self._database.transaction() as connection:
            return self.enqueue_in_transaction(connection, kind, dedup_key, payload, stage)

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        kind: str,
        dedup_key: str,
        payload: Mapping[str, JsonValue],
        stage: str = "pending",
    ) -> JobId:
        effective_payload = dict(payload)
        if kind == "parse_pdf" and "request_revision" not in effective_payload:
            content_id = effective_payload.get("content_id")
            if not isinstance(content_id, str):
                content_id = effective_payload.get("content_sha256")
            if isinstance(content_id, str):
                active = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE dedup_key=? AND status IN ('queued','running')
                    """,
                    (dedup_key,),
                ).fetchone()
                if active is not None:
                    return JobId(str(active["id"]))
                allocated = connection.execute(
                    """
                    UPDATE contents
                    SET parse_request_revision=parse_request_revision+1
                    WHERE sha256=?
                    RETURNING parse_request_revision
                    """,
                    (content_id,),
                ).fetchone()
                if allocated is None:
                    raise ValueError("parse_content_not_found")
                effective_payload["request_revision"] = int(
                    allocated["parse_request_revision"]
                )
        job_id = JobId(str(uuid4()))
        timestamp = self._timestamp(self._now_utc())
        try:
            connection.execute(
                """
                INSERT INTO jobs (
                  id, kind, dedup_key, payload_json, status, stage, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    dedup_key,
                    json.dumps(
                        effective_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    stage,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            active = connection.execute(
                """
                SELECT id FROM jobs
                WHERE dedup_key = ? AND status IN ('queued', 'running')
                """,
                (dedup_key,),
            ).fetchone()
            if active is None:
                raise
            return JobId(str(active["id"]))
        return job_id

    def lease_next(self, worker_id: str, lease_seconds: int) -> Job | None:
        expiry = self._lease_expiry(lease_seconds)
        timestamp = self._timestamp(self._now_utc())
        with self._database.transaction() as connection:
            selected = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status = 'queued'
                ORDER BY
                  CASE
                    WHEN kind = 'translate_selection' THEN 0
                    WHEN kind = 'parse_pdf'
                         AND json_type(payload_json, '$.document_id') = 'text' THEN 1
                    WHEN kind IN ('validate_import', 'scan_library') THEN 1
                    WHEN kind = 'parse_pdf' THEN 3
                    ELSE 2
                  END,
                  created_at, id
                LIMIT 1
                """
            ).fetchone()
            if selected is None:
                return None
            job_id = str(selected["id"])
            changed = connection.execute(
                """
                UPDATE jobs
                SET status = 'running', attempts = attempts + 1, lease_owner = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, self._timestamp(expiry), timestamp, job_id),
            )
            if changed.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise RuntimeError("leased job disappeared")
            return self._row_to_job(row)

    def renew_lease(self, job_id: JobId, worker_id: str, lease_seconds: int) -> datetime:
        expiry = self._lease_expiry(lease_seconds)
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    self._timestamp(expiry),
                    self._timestamp(self._now_utc()),
                    job_id,
                    worker_id,
                ),
            )
            self._require_lease(changed, job_id, worker_id)
        return expiry

    def require_current_lease(
        self, connection: sqlite3.Connection, job_id: JobId, worker_id: str
    ) -> None:
        row = connection.execute(
            """
            SELECT status,lease_owner,lease_expires_at FROM jobs WHERE id=?
            """,
            (job_id,),
        ).fetchone()
        if (
            row is None
            or str(row["status"]) != "running"
            or str(row["lease_owner"]) != worker_id
            or row["lease_expires_at"] is None
            or datetime.fromisoformat(str(row["lease_expires_at"])) <= self._now_utc()
        ):
            raise LostLease(f"job {job_id} is not currently leased by {worker_id}")

    def checkpoint(
        self,
        job_id: JobId,
        worker_id: str,
        stage: str,
        payload: Mapping[str, JsonValue],
    ) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET stage = ?, checkpoint_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    stage,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    self._timestamp(self._now_utc()),
                    job_id,
                    worker_id,
                ),
            )
            self._require_lease(changed, job_id, worker_id)

    def complete(self, job_id: JobId, worker_id: str) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (self._timestamp(self._now_utc()), job_id, worker_id),
            )
            self._require_lease(changed, job_id, worker_id)

    def fail(
        self,
        job_id: JobId,
        worker_id: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', error_code = ?, retryable = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    error_code,
                    int(retryable),
                    self._timestamp(self._now_utc()),
                    job_id,
                    worker_id,
                ),
            )
            self._require_lease(changed, job_id, worker_id)

    def requeue_running(self, job_id: JobId, worker_id: str) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    created_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    self._timestamp(self._now_utc()),
                    self._timestamp(self._now_utc()),
                    job_id,
                    worker_id,
                ),
            )
            self._require_lease(changed, job_id, worker_id)

    def requeue_expired(self) -> int:
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE status = 'running' AND retryable = 1
                  AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (
                    self._timestamp(self._now_utc()),
                    self._timestamp(self._now_utc()),
                ),
            )
            return changed.rowcount

    def count(self, kind: str | None = None, status: str | None = None) -> int:
        clauses: list[str] = []
        parameters: list[str] = []
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._database.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM jobs{where}", parameters
            ).fetchone()
        if row is None:
            raise RuntimeError("count query returned no row")
        return int(row["count"])

    def _lease_expiry(self, lease_seconds: int) -> datetime:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return self._now_utc() + timedelta(seconds=lease_seconds)

    def _now_utc(self) -> datetime:
        current = self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("queue clock must return an aware datetime")
        return current.astimezone(UTC)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _require_lease(
        result: sqlite3.Cursor, job_id: JobId, worker_id: str
    ) -> None:
        if result.rowcount != 1:
            raise LostLease(f"job {job_id} is not leased by {worker_id}")

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        payload = json.loads(str(row["payload_json"]))
        checkpoint = json.loads(str(row["checkpoint_json"]))
        if not isinstance(payload, dict) or not isinstance(checkpoint, dict):
            raise ValueError("job payload and checkpoint must be JSON objects")
        expiry_text = row["lease_expires_at"]
        expiry = None if expiry_text is None else datetime.fromisoformat(str(expiry_text))
        return Job(
            id=JobId(str(row["id"])),
            kind=str(row["kind"]),
            dedup_key=str(row["dedup_key"]),
            payload=cast(dict[str, JsonValue], payload),
            status=cast(JobStatus, str(row["status"])),
            stage=str(row["stage"]),
            checkpoint=cast(dict[str, JsonValue], checkpoint),
            attempts=int(row["attempts"]),
            lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
            lease_expires_at=expiry,
        )
