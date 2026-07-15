from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from literature_workbench.db.database import Database


ImportStatus = Literal[
    "receiving",
    "uploaded",
    "validating",
    "ready_to_promote",
    "completed",
    "failed",
]


@dataclass(frozen=True, slots=True)
class CreatedImportIntent:
    intent_id: UUID
    upload_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedImportIntent:
    intent_id: UUID
    original_name: str
    expected_size: int


@dataclass(frozen=True, slots=True)
class ImportTask:
    intent_id: UUID
    file_name: str
    status: ImportStatus
    document_id: UUID | None
    error_code: str | None
    poll_after_ms: int | None


def safe_display_name(original_name: str) -> str:
    clean = "".join(
        character
        for character in original_name
        if not unicodedata.category(character).startswith("C")
    )
    return Path(clean).name[:255] or "document.pdf"


class ImportIntentRepository:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self._database = database
        self._now = now

    def create(
        self,
        session_generation: str,
        original_name: str,
        expected_size: int,
        media_type: str,
    ) -> CreatedImportIntent:
        if not 0 < expected_size <= 2 * 1024**3:
            raise ValueError("invalid_import_size")
        if media_type != "application/pdf":
            raise ValueError("invalid_import_media_type")
        intent_id = uuid4()
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._aware_now()
        expires = now + timedelta(minutes=15)
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO import_intents (
                  id, session_generation, file_name, expected_size, expected_media_type,
                  upload_token_hash, status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    str(intent_id),
                    session_generation,
                    safe_display_name(original_name),
                    expected_size,
                    media_type,
                    digest,
                    expires.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return CreatedImportIntent(intent_id, token, expires)

    def claim(
        self, intent_id: UUID, session_generation: str, upload_token: str
    ) -> ClaimedImportIntent:
        now = self._aware_now()
        candidate = hashlib.sha256(upload_token.encode("utf-8")).hexdigest()
        claimed: ClaimedImportIntent | None = None
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM import_intents WHERE id = ? AND session_generation = ?",
                (str(intent_id), session_generation),
            ).fetchone()
            if row is None or str(row["status"]) != "pending":
                raise KeyError("import_not_found")
            stored = str(row["upload_token_hash"])
            expires = datetime.fromisoformat(str(row["expires_at"]))
            if now >= expires:
                connection.execute(
                    """
                    UPDATE import_intents SET status = 'failed', upload_token_hash = NULL,
                      error_code = 'intent_expired', updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now.isoformat(), str(intent_id)),
                )
            elif not hmac.compare_digest(stored, candidate):
                raise KeyError("import_not_found")
            else:
                changed = connection.execute(
                    """
                    UPDATE import_intents SET status = 'receiving', upload_token_hash = NULL,
                      consumed_at = ?, updated_at = ?
                    WHERE id = ? AND session_generation = ? AND status = 'pending'
                      AND upload_token_hash = ?
                    """,
                    (
                        now.isoformat(),
                        now.isoformat(),
                        str(intent_id),
                        session_generation,
                        stored,
                    ),
                )
                if changed.rowcount != 1:
                    raise KeyError("import_not_found")
                claimed = ClaimedImportIntent(
                    UUID(str(row["id"])),
                    str(row["file_name"]),
                    int(row["expected_size"]),
                )
        if claimed is None:
            raise KeyError("import_not_found")
        return claimed

    def expire_pending(self) -> int:
        now = self._aware_now().isoformat()
        with self._database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE import_intents
                SET status='failed', upload_token_hash=NULL, error_code='intent_expired',
                    updated_at=?
                WHERE status='pending' AND expires_at <= ?
                """,
                (now, now),
            )
        return changed.rowcount

    def fail(self, intent_id: UUID, error_code: str, *, allowed: tuple[str, ...] = ()) -> None:
        now = self._aware_now().isoformat()
        where = " AND status IN (%s)" % ",".join("?" for _ in allowed) if allowed else ""
        with self._database.transaction() as connection:
            connection.execute(
                f"UPDATE import_intents SET status='failed', upload_token_hash=NULL, error_code=?, updated_at=? WHERE id=?{where}",
                (error_code, now, str(intent_id), *allowed),
            )

    def get(self, intent_id: UUID, session_generation: str) -> ImportTask:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT i.*, c.document_id FROM import_intents AS i
                LEFT JOIN confirmed_imports AS c ON c.intent_id = i.id
                WHERE i.id = ? AND i.session_generation = ?
                """,
                (str(intent_id), session_generation),
            ).fetchone()
        if row is None or str(row["status"]) == "pending":
            raise KeyError("import_not_found")
        return self._view(row)

    def list_current(
        self, session_generation: str, *, active: bool = True
    ) -> tuple[ImportTask, ...]:
        recent_cutoff = (self._aware_now() - timedelta(hours=24)).isoformat()
        recent_clause = (
            "AND (i.status IN ('receiving','uploaded','validating','ready_to_promote') "
            "OR i.updated_at >= ?)"
            if active
            else ""
        )
        parameters: tuple[str, ...] = (
            (session_generation, recent_cutoff) if active else (session_generation,)
        )
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT i.*, c.document_id FROM import_intents AS i
                LEFT JOIN confirmed_imports AS c ON c.intent_id = i.id
                WHERE i.session_generation = ? AND i.status <> 'pending'
                {recent_clause}
                ORDER BY i.created_at DESC, i.id
                LIMIT 100
                """,
                parameters,
            ).fetchall()
        return tuple(self._view(row) for row in rows)

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("import clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _view(row: sqlite3.Row) -> ImportTask:
        status = cast(ImportStatus, str(row["status"]))
        terminal = status in {"completed", "failed"}
        document = row["document_id"]
        return ImportTask(
            UUID(str(row["id"])),
            str(row["file_name"]),
            status,
            UUID(str(document)) if status == "completed" and document is not None else None,
            str(row["error_code"]) if status == "failed" and row["error_code"] else None,
            None if terminal else 250,
        )
