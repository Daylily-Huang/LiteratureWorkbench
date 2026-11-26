from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from literature_workbench.db.database import Database
from literature_workbench.db.task_queue import TaskQueue
from literature_workbench.features.translation.reuse import TranslationPiece


PROMPT_VERSION = "selection-translation-v2"
MAX_CONTEXT_CODE_POINTS = 500
MIN_PROSE_BLOCK_WORDS = 8
TranslationStatus = Literal["queued", "running", "completed", "failed"]


class SelectionRejected(ValueError):
    pass


class TranslationNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedSelection:
    document_id: UUID
    document_version_id: UUID
    content_id: str
    physical_page: int
    selected_text: str
    context_before: str
    context_after: str
    target_language: Literal["zh-CN"]
    page_text_hash: str = ""


@dataclass(frozen=True, slots=True)
class TranslationTask:
    id: UUID
    client_task_id: UUID
    document_id: UUID
    document_version_id: UUID
    content_id: str
    physical_page: int
    selected_text: str
    context_before: str
    context_after: str
    target_language: Literal["zh-CN"]
    route_mode: Literal["online", "local_only"]
    requested_provider: str
    requested_model: str
    settings_revision: int
    prompt_version: str
    status: TranslationStatus
    provider: str | None
    model: str | None
    fallback_reason: str | None
    result_cache_key: str | None
    error_code: str | None
    translated_text: str | None
    detected_source_language: str | None
    page_text_hash: str = ""
    prefer_exact: bool = False
    reuse_details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CachedTranslation:
    cache_key: str
    translated_text: str
    detected_source_language: str
    provider: str
    model: str


def normalize_selection(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value, flags=re.UNICODE).strip()
    normalized = re.sub(r"\s+([,.;:!?%\)\]\}，。；：！？％、])", r"\1", normalized)
    return re.sub(r"([\(\[\{（【])\s+", r"\1", normalized)


def is_prose_block(value: str) -> bool:
    """Return whether a parsed block is substantial enough to be running prose.

    PDF parser reading order contains figure labels as independent blocks.  The
    browser text selection naturally omits those small labels when a user
    drags through a paragraph around a figure, so verification also keeps a
    prose-only flow.  Short normal prose is deliberately not skipped: it stays
    out of this fallback and therefore cannot be silently bypassed.
    """
    return len(re.findall(r"\w+", value, flags=re.UNICODE)) >= MIN_PROSE_BLOCK_WORDS


def current_page_text_candidates(row: sqlite3.Row) -> list[str]:
    candidates = [
        normalize_selection(str(row["normalized_text"] or row["raw_text"])),
        normalize_selection(str(row["raw_text"])),
    ]
    try:
        raw_blocks = json.loads(str(row["blocks_json"]))
    except (json.JSONDecodeError, TypeError):
        raw_blocks = []
    block_texts = [
        normalize_selection(block["text"])
        for block in raw_blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ] if isinstance(raw_blocks, list) else []
    candidates.extend(text for text in block_texts if text)
    joined_blocks = normalize_selection(" ".join(block_texts))
    if joined_blocks:
        candidates.append(joined_blocks)
    joined_prose_blocks = normalize_selection(
        " ".join(text for text in block_texts if is_prose_block(text)),
    )
    if joined_prose_blocks:
        candidates.append(joined_prose_blocks)
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def align_selection_to_candidate(candidate: str, selection: str) -> tuple[str, int] | None:
    position = candidate.find(selection)
    if position >= 0:
        return selection, position
    compact_selection = "".join(character for character in selection if not character.isspace())
    if not compact_selection:
        return None
    compact_candidate_characters: list[str] = []
    candidate_positions: list[int] = []
    for index, character in enumerate(candidate):
        if character.isspace():
            continue
        compact_candidate_characters.append(character)
        candidate_positions.append(index)
    compact_candidate = "".join(compact_candidate_characters)
    compact_position = compact_candidate.find(compact_selection)
    if compact_position >= 0:
        last_compact_position = compact_position + len(compact_selection) - 1
        start = candidate_positions[compact_position]
        end = candidate_positions[last_compact_position] + 1
        return candidate[start:end], start
    # NFKC fallback handles PDF ligatures (e.g. 'ﬁ' <-> 'fi', 'ﬂ' <-> 'fl') even for mid-word drags
    nfkc_selection = "".join(
        ch for ch in unicodedata.normalize("NFKC", selection) if not ch.isspace()
    )
    if not nfkc_selection:
        return None
    nfkc_chars: list[str] = []
    nfkc_positions: list[int] = []
    for index, character in enumerate(candidate):
        if character.isspace():
            continue
        for expanded in unicodedata.normalize("NFKC", character):
            if expanded.isspace():
                continue
            nfkc_chars.append(expanded)
            nfkc_positions.append(index)
    nfkc_candidate = "".join(nfkc_chars)
    nfkc_pos = nfkc_candidate.find(nfkc_selection)
    if nfkc_pos < 0:
        return None
    start = nfkc_positions[nfkc_pos]
    end = nfkc_positions[nfkc_pos + len(nfkc_selection) - 1] + 1
    return candidate[start:end], start


_FUZZY_MIN_TOKENS = 3
_FUZZY_MIN_COVERAGE = 0.9
_FUZZY_WINDOW_FACTOR = 1.25
_FUZZY_MAX_ANCHORS = 64
_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _fuzzy_tokens(value: str) -> list[tuple[str, int]]:
    """Tokenize normalized page text for order-tolerant matching.

    Returns ``(token, char_offset)`` pairs.  Latin runs become word tokens;
    CJK runs become overlapping character bigrams (a lone character stays a
    single token).  Text is NFKC-normalized and case-folded first so
    ligatures, full-width forms, and styled punctuation do not defeat
    matching between the browser text layer and the parser output.
    """
    key = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[tuple[str, int]] = []
    word_start: int | None = None
    cjk_run: list[tuple[str, int]] = []

    def flush_word(end: int) -> None:
        nonlocal word_start
        if word_start is not None:
            tokens.append((key[word_start:end], word_start))
            word_start = None

    def flush_cjk() -> None:
        if len(cjk_run) == 1:
            tokens.append(cjk_run[0])
        else:
            for first, second in zip(cjk_run, cjk_run[1:], strict=False):
                tokens.append((first[0] + second[0], first[1]))
        cjk_run.clear()

    for index, character in enumerate(key):
        if character.isspace():
            flush_word(index)
            flush_cjk()
        elif _CJK_CHAR.match(character) is not None:
            flush_word(index)
            cjk_run.append((character, index))
        elif character.isalnum():
            flush_cjk()
            if word_start is None:
                word_start = index
        else:
            flush_word(index)
            flush_cjk()
    flush_word(len(key))
    flush_cjk()
    return tokens


def align_selection_fuzzy(candidate: str, selection: str) -> tuple[str, int] | None:
    """Order-tolerant alignment for capture/parser divergence.

    The browser text layer and the parser can disagree about reading order
    or drop small fragments (figure labels, footer lines), so exact
    containment fails even though the user selected genuine page text.
    This fallback anchors a window of candidate tokens at each occurrence
    of the selection's first token and accepts the best window when at
    least 90% of the selection's tokens appear inside it (compared as a
    multiset, so order may differ).  The tight window cap keeps a
    selection that skips a whole intervening prose block from passing.
    """
    selection_tokens = _fuzzy_tokens(selection)
    if len(selection_tokens) < _FUZZY_MIN_TOKENS:
        return None
    candidate_tokens = _fuzzy_tokens(candidate)
    if not candidate_tokens:
        return None
    window_size = min(
        len(candidate_tokens),
        max(
            len(selection_tokens) + 2,
            math.ceil(len(selection_tokens) * _FUZZY_WINDOW_FACTOR),
        ),
    )
    needed = math.ceil(len(selection_tokens) * _FUZZY_MIN_COVERAGE)
    first_token = selection_tokens[0][0]
    best_offset: int | None = None
    best_matched = 0
    anchors = 0
    for index, (token, offset) in enumerate(candidate_tokens):
        if token != first_token:
            continue
        anchors += 1
        if anchors > _FUZZY_MAX_ANCHORS:
            break
        counts: dict[str, int] = {}
        for window_token, _ in candidate_tokens[index : index + window_size]:
            counts[window_token] = counts.get(window_token, 0) + 1
        matched = 0
        for selection_token, _ in selection_tokens:
            if counts.get(selection_token, 0) > 0:
                counts[selection_token] -= 1
                matched += 1
        if matched >= needed and matched > best_matched:
            best_matched = matched
            best_offset = offset
    if best_offset is None:
        return None
    return selection, best_offset


def match_selection_to_candidates(
    candidates: list[str], selection: str
) -> tuple[str, tuple[str, int]] | None:
    """Return the first candidate aligning with ``selection`` and its match.

    Exact containment is preferred across every candidate; the
    order-tolerant fallback runs only when no candidate contains the
    selection, so previously accepted selections keep their context.
    """
    for candidate in candidates:
        aligned = align_selection_to_candidate(candidate, selection)
        if aligned is not None:
            return candidate, aligned
    for candidate in candidates:
        aligned = align_selection_fuzzy(candidate, selection)
        if aligned is not None:
            return candidate, aligned
    return None


class TranslationRepository:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self._database = database
        self._now = now

    def verify_selection(
        self,
        *,
        document_id: UUID,
        document_version_id: UUID,
        content_id: str,
        physical_page: int,
        selected_text: str,
        target_language: Literal["zh-CN"],
    ) -> VerifiedSelection:
        normalized_selection = normalize_selection(selected_text)
        if not normalized_selection or len(normalized_selection) > 8_000:
            raise SelectionRejected("selection_not_current")
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT p.raw_text,p.normalized_text,p.blocks_json
                FROM documents AS d
                JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
                JOIN document_versions AS v
                  ON v.id=? AND v.document_id=d.id AND v.content_sha256=d.content_sha256
                 AND v.id=(
                   SELECT latest.id FROM document_versions AS latest
                   WHERE latest.document_id=d.id
                   ORDER BY latest.observed_at DESC,latest.rowid DESC LIMIT 1
                 )
                JOIN contents AS c
                  ON c.sha256=d.content_sha256 AND c.mime_type='application/pdf'
                JOIN parse_runs AS r
                  ON r.id=c.current_parse_run_id
                 AND r.content_sha256=c.sha256
                 AND r.status='completed'
                JOIN pages AS p
                  ON p.parse_run_id=r.id
                 AND p.content_sha256=c.sha256
                 AND p.physical_page=?
                WHERE d.id=? AND d.content_sha256=? AND d.format='pdf'
                  AND d.status IN ('candidate_pdf','valid_pdf')
                """,
                (
                    str(document_version_id),
                    physical_page,
                    str(document_id),
                    content_id,
                ),
            ).fetchone()
            if row is None:
                raise SelectionRejected(
                    self._diagnose_rejection(
                        connection,
                        document_id=document_id,
                        document_version_id=document_version_id,
                        content_id=content_id,
                    )
                )
        match = match_selection_to_candidates(
            current_page_text_candidates(row), normalized_selection
        )
        if match is None:
            raise SelectionRejected("selection_text_mismatch")
        matching_text, (normalized_selection, position) = match
        before = matching_text[:position].rstrip()
        after = matching_text[position + len(normalized_selection) :].lstrip()
        return VerifiedSelection(
            document_id=document_id,
            document_version_id=document_version_id,
            content_id=content_id,
            physical_page=physical_page,
            selected_text=normalized_selection,
            context_before="".join(list(before)[-MAX_CONTEXT_CODE_POINTS:]),
            context_after="".join(list(after)[:MAX_CONTEXT_CODE_POINTS]),
            target_language=target_language,
            page_text_hash=page_text_fingerprint(row),
        )

    @staticmethod
    def _diagnose_rejection(
        connection: sqlite3.Connection,
        *,
        document_id: UUID,
        document_version_id: UUID,
        content_id: str,
    ) -> str:
        """Explain why the verification join found no usable page row.

        Runs only on the rejection path, so the extra queries cost nothing
        for accepted selections.  The order matters: a stale version is the
        most common and most recoverable cause, an unfinished parse is
        permanent until the parser completes, and anything else means the
        document identity itself no longer resolves.
        """
        document = connection.execute(
            """
            SELECT d.content_sha256 AS current_content, d.status AS document_status,
                   (SELECT latest.id FROM document_versions AS latest
                    WHERE latest.document_id=d.id
                    ORDER BY latest.observed_at DESC,latest.rowid DESC LIMIT 1
                   ) AS latest_version_id
            FROM documents AS d
            JOIN libraries AS l ON l.id=d.library_id AND l.status='active'
            WHERE d.id=? AND d.format='pdf'
            """,
            (str(document_id),),
        ).fetchone()
        if document is None:
            return "selection_not_current"
        if str(document["latest_version_id"] or "") != str(document_version_id):
            return "selection_version_stale"
        if str(document["current_content"]) != content_id or str(
            document["document_status"]
        ) not in ("candidate_pdf", "valid_pdf"):
            return "selection_not_current"
        parse = connection.execute(
            """
            SELECT r.status AS run_status
            FROM contents AS c
            LEFT JOIN parse_runs AS r ON r.id=c.current_parse_run_id
            WHERE c.sha256=? AND c.mime_type='application/pdf'
            """,
            (content_id,),
        ).fetchone()
        if parse is None or parse["run_status"] != "completed":
            return "selection_parse_pending"
        return "selection_not_current"

    def create(
        self,
        *,
        session_generation: str,
        client_task_id: UUID,
        selection: VerifiedSelection,
        route_mode: Literal["online", "local_only"],
        requested_provider: str,
        requested_model: str,
        settings_revision: int,
        queue: TaskQueue,
        prefer_exact: bool = False,
        prompt_version: str | None = None,
    ) -> TranslationTask:
        timestamp = self._timestamp()
        task_id = uuid4()
        effective_version = prompt_version or PROMPT_VERSION
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM translation_tasks WHERE session_generation=? AND client_task_id=?",
                (session_generation, str(client_task_id)),
            ).fetchone()
            if existing is not None:
                return self.get_for_worker(UUID(str(existing["id"])))
            connection.execute(
                """
                INSERT INTO translation_tasks(
                  id,session_generation,client_task_id,document_id,document_version_id,
                  content_sha256,physical_page,selected_text,context_before,context_after,
                  target_language,route_mode,requested_provider,requested_model,
                  settings_revision,prompt_version,status,stage,created_at,updated_at,page_text_hash,prefer_exact)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued','pending',?,?,?,?)
                """,
                (
                    str(task_id), session_generation, str(client_task_id),
                    str(selection.document_id), str(selection.document_version_id),
                    selection.content_id, selection.physical_page, selection.selected_text,
                    selection.context_before, selection.context_after,
                    selection.target_language, route_mode, requested_provider,
                    requested_model, settings_revision, effective_version, timestamp, timestamp,
                    selection.page_text_hash, int(prefer_exact),
                ),
            )
            queue.enqueue_in_transaction(
                connection,
                "translate_selection",
                f"translation:{task_id}",
                {
                    "task_id": str(task_id),
                    "document_id": str(selection.document_id),
                    "document_version_id": str(selection.document_version_id),
                    "content_id": selection.content_id,
                    "physical_page": selection.physical_page,
                    "selected_text": selection.selected_text,
                    "target_language": selection.target_language,
                    "prompt_version": effective_version,
                },
            )
        return self.get_for_worker(task_id)

    def read_paper_context(self, document_id: UUID, content_id: str, physical_page: int) -> tuple[str, str]:
        """Return (paper_title, paper_opening_excerpt) from server-verified records."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.display_name, c.title, c.current_parse_run_id
                FROM documents AS d
                JOIN contents AS c ON c.sha256 = d.content_sha256
                WHERE d.id = ? AND d.content_sha256 = ?
                """,
                (str(document_id), content_id),
            ).fetchone()
            if row is None:
                return "", ""
            title = str(row["title"] or "").strip()
            if not title:
                display = str(row["display_name"] or "").strip()
                title = display[:-4] if display.lower().endswith(".pdf") else display
            opening = ""
            if physical_page > 1 and row["current_parse_run_id"]:
                first_page = connection.execute(
                    """
                    SELECT normalized_text, raw_text FROM pages
                    WHERE content_sha256 = ? AND parse_run_id = ? AND physical_page = 1
                    """,
                    (content_id, str(row["current_parse_run_id"])),
                ).fetchone()
                if first_page is not None:
                    opening = str(first_page["normalized_text"] or first_page["raw_text"] or "")
        return title, opening

    def get(
        self, task_id: UUID, *, session_generation: str
    ) -> TranslationTask:
        return self._get(task_id, session_generation=session_generation)

    def get_for_worker(self, task_id: UUID) -> TranslationTask:
        return self._get(task_id, session_generation=None)

    def expire_other_sessions(self, current_session_generation: str) -> int:
        timestamp = self._timestamp()
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM translation_tasks
                WHERE session_generation<>? AND status IN ('queued','running')
                """,
                (current_session_generation,),
            ).fetchall()
            for row in rows:
                task_id = str(row["id"])
                connection.execute(
                    """
                    UPDATE translation_tasks
                    SET status='failed',stage='failed',error_code='translation_session_expired',
                        updated_at=?
                    WHERE id=? AND status IN ('queued','running')
                    """,
                    (timestamp, task_id),
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='failed',stage='failed',error_code='translation_session_expired',
                        retryable=0,lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE dedup_key=? AND status IN ('queued','running')
                    """,
                    (timestamp, f"translation:{task_id}"),
                )
        return len(rows)

    def _get(
        self, task_id: UUID, *, session_generation: str | None
    ) -> TranslationTask:
        session_clause = "" if session_generation is None else " AND t.session_generation=?"
        parameters = (
            (str(task_id),)
            if session_generation is None
            else (str(task_id), session_generation)
        )
        with self._database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT t.*,c.translated_text,c.detected_source_language
                FROM translation_tasks AS t
                LEFT JOIN translation_cache AS c ON c.cache_key=t.result_cache_key
                WHERE t.id=?{session_clause}
                """,  # noqa: S608 - clause is selected only from fixed server literals
                parameters,
            ).fetchone()
        if row is None:
            raise TranslationNotFound("translation_not_found")
        return self._task_from_row(row)

    def mark_running(self, task_id: UUID) -> TranslationTask:
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE translation_tasks SET status='running',stage='model_request',
                   updated_at=? WHERE id=? AND status='queued'""",
                (self._timestamp(), str(task_id)),
            )
        return self.get_for_worker(task_id)

    def cache_key(self, task: TranslationTask, provider: str, model: str) -> str:
        stable = json.dumps(
            {
                "document_id": str(task.document_id),
                "page_text_hash": task.page_text_hash,
                "physical_page": task.physical_page,
                "selected_text": normalize_selection(task.selected_text),
                "target_language": task.target_language,
                "prompt_version": task.prompt_version,
                "provider": provider,
                "model": model,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(stable).hexdigest()

    def cached(self, cache_key: str) -> CachedTranslation | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM translation_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
        if row is None:
            return None
        return CachedTranslation(
            cache_key=cache_key,
            translated_text=str(row["translated_text"]),
            detected_source_language=str(row["detected_source_language"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
        )

    def complete(
        self,
        task_id: UUID,
        *,
        cache_key: str,
        translated_text: str,
        detected_source_language: str,
        provider: str,
        model: str,
        fallback_reason: str | None,
    ) -> TranslationTask:
        task = self.get_for_worker(task_id)
        timestamp = self._timestamp()
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO translation_cache(
                   cache_key,translated_text,detected_source_language,target_language,
                   provider,model,prompt_version,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (cache_key, translated_text, detected_source_language, task.target_language,
                 provider, model, task.prompt_version, timestamp),
            )
            connection.execute(
                """UPDATE translation_tasks SET status='completed',stage='completed',
                   provider=?,model=?,fallback_reason=?,result_cache_key=?,error_code=NULL,
                   updated_at=? WHERE id=? AND status='running'""",
                (provider, model, fallback_reason, cache_key, timestamp, str(task_id)),
            )
        return self.get_for_worker(task_id)

    def complete_cached(
        self, task_id: UUID, cached: CachedTranslation, fallback_reason: str | None = None
    ) -> TranslationTask:
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE translation_tasks SET status='completed',stage='cache_hit',
                   provider=?,model=?,fallback_reason=?,result_cache_key=?,error_code=NULL,
                   updated_at=? WHERE id=? AND status='running'""",
                (cached.provider, cached.model, fallback_reason, cached.cache_key,
                 self._timestamp(), str(task_id)),
            )
        return self.get_for_worker(task_id)

    def fail(self, task_id: UUID, error_code: str) -> TranslationTask:
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE translation_tasks SET status='failed',stage='failed',error_code=?,
                   updated_at=? WHERE id=? AND status='running'""",
                (error_code, self._timestamp(), str(task_id)),
            )
        return self.get_for_worker(task_id)

    def set_reuse_details(self, task_id: UUID, details: dict[str, object]) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE translation_tasks SET reuse_details=? WHERE id=? AND status='running'",
                (json.dumps(details, ensure_ascii=False), str(task_id)),
            )

    def remember(self, task: TranslationTask, provider: str, model: str, piece: TranslationPiece) -> None:
        if not task.page_text_hash or not piece.source or not piece.translation:
            return
        key = hashlib.sha256(json.dumps([
            str(task.document_id), task.physical_page, task.page_text_hash, piece.source,
            provider, model, task.target_language, task.prompt_version,
        ], ensure_ascii=False).encode()).hexdigest()
        with self._database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO translation_memory VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, str(task.document_id), task.physical_page, task.page_text_hash,
                 piece.source, piece.translation, provider, model, task.target_language,
                 task.prompt_version, str(task.id), self._timestamp()),
            )

    def memories(self, task: TranslationTask, provider: str, model: str) -> list[TranslationPiece]:
        if not task.page_text_hash:
            return []
        scope = (str(task.document_id), task.physical_page, provider, model,
                 task.target_language, task.prompt_version)
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT source_text,translated_text FROM translation_memory
                   WHERE document_id=? AND physical_page=? AND provider=? AND model=?
                     AND target_language=? AND prompt_version=? AND page_text_hash=?
                   ORDER BY created_at DESC LIMIT 500""", (*scope, task.page_text_hash),
            ).fetchall()
            result = {str(row["source_text"]): str(row["translated_text"]) for row in reversed(rows)}
            # Old successful translations remain usable. Reconstruct their page
            # fingerprint from the retained parse snapshot, never from a new file.
            legacy = connection.execute(
                """SELECT t.selected_text,t.context_before,t.context_after,t.content_sha256,c.translated_text FROM translation_tasks t
                   JOIN translation_cache c ON c.cache_key=t.result_cache_key
                   WHERE t.document_id=? AND t.physical_page=? AND t.provider=? AND t.model=?
                     AND t.target_language=? AND t.prompt_version=? AND t.status='completed'
                     AND t.page_text_hash='' ORDER BY t.updated_at DESC LIMIT 500""", scope,
            ).fetchall()
            hashes: dict[str, str] = {}
            page_texts: dict[str, list[str]] = {}
            for row in legacy:
                content = str(row["content_sha256"])
                if content not in hashes:
                    page = connection.execute(
                        """SELECT p.raw_text,p.normalized_text,p.blocks_json FROM pages p
                           JOIN contents c ON c.sha256=p.content_sha256 AND c.current_parse_run_id=p.parse_run_id
                           JOIN parse_runs r ON r.id=p.parse_run_id AND r.status='completed'
                           WHERE p.content_sha256=? AND p.physical_page=?""", (content, task.physical_page),
                    ).fetchone()
                    hashes[content] = "" if page is None else page_text_fingerprint(page)
                    page_texts[content] = [] if page is None else current_page_text_candidates(page)
                context = normalize_selection(" ".join(str(row[key]) for key in ("context_before", "selected_text", "context_after")))
                if hashes[content] == task.page_text_hash and any(context in normalize_selection(text) for text in page_texts[content]):
                    result.setdefault(normalize_selection(str(row["selected_text"])), str(row["translated_text"]))
        return [TranslationPiece(source, translation) for source, translation in result.items() if source and translation]

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TranslationTask:
        return TranslationTask(
            id=UUID(str(row["id"])), client_task_id=UUID(str(row["client_task_id"])),
            document_id=UUID(str(row["document_id"])),
            document_version_id=UUID(str(row["document_version_id"])),
            content_id=str(row["content_sha256"]), physical_page=int(row["physical_page"]),
            selected_text=str(row["selected_text"]), context_before=str(row["context_before"]),
            context_after=str(row["context_after"]), target_language="zh-CN",
            route_mode=str(row["route_mode"]),  # type: ignore[arg-type]
            requested_provider=str(row["requested_provider"]),
            requested_model=str(row["requested_model"]),
            settings_revision=int(row["settings_revision"]), prompt_version=str(row["prompt_version"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            provider=None if row["provider"] is None else str(row["provider"]),
            model=None if row["model"] is None else str(row["model"]),
            fallback_reason=None if row["fallback_reason"] is None else str(row["fallback_reason"]),
            result_cache_key=None if row["result_cache_key"] is None else str(row["result_cache_key"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            translated_text=None if row["translated_text"] is None else str(row["translated_text"]),
            detected_source_language=None if row["detected_source_language"] is None else str(row["detected_source_language"]),
            page_text_hash=str(row["page_text_hash"]), prefer_exact=bool(row["prefer_exact"]),
            reuse_details=json.loads(str(row["reuse_details"])),
        )


def page_text_fingerprint(row: sqlite3.Row) -> str:
    candidates = sorted({normalize_selection(text) for text in current_page_text_candidates(row)})
    return hashlib.sha256(json.dumps(candidates, ensure_ascii=False).encode("utf-8")).hexdigest()
