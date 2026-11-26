from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask

from literature_workbench.db.database import Database

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


class ReadingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int | None = Field(default=None, ge=1, le=100_000)
    totalPages: int | None = Field(default=None, ge=0, le=100_000)
    lastOpened: int | None = Field(default=None, ge=0, le=9_007_199_254_740_991)
    favorite: bool | None = None
    readingStatus: str | None = Field(default=None, pattern="^(unread|reading|read)$")
    tags: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def validate_patch(self) -> ReadingPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("null_not_allowed")
        if self.tags is not None:
            self.tags = list(dict.fromkeys(tag.strip() for tag in self.tags if tag.strip()))
            if any(len(tag) > 60 for tag in self.tags):
                raise ValueError("tag_too_long")
        return self


def _reading(row: sqlite3.Row) -> dict[str, object]:
    return {"page": row["page"], "totalPages": row["total_pages"],
            "lastOpened": row["last_opened"], "favorite": bool(row["favorite"]),
            "readingStatus": row["reading_status"], "tags": json.loads(row["tags_json"])}


@router.get("/reading")
def reading_records(request: Request) -> JSONResponse:
    with request.app.state.database.connect() as connection:
        rows = connection.execute("SELECT * FROM reading_records").fetchall()
    return JSONResponse({"items": {str(row["document_id"]): _reading(row) for row in rows}})


@router.patch("/reading/{document_id}")
def update_reading(request: Request, document_id: UUID, payload: ReadingPatch) -> JSONResponse:
    columns = {"page": "page", "totalPages": "total_pages", "lastOpened": "last_opened",
               "favorite": "favorite", "readingStatus": "reading_status", "tags": "tags_json"}
    with request.app.state.database.transaction() as connection:
        if connection.execute("SELECT 1 FROM documents WHERE id=?", (str(document_id),)).fetchone() is None:
            return JSONResponse({"code": "document_not_found"}, status_code=404)
        connection.execute("INSERT OR IGNORE INTO reading_records(document_id) VALUES(?)", (str(document_id),))
        for key, value in payload.model_dump(exclude_unset=True).items():
            column = columns[key]
            connection.execute(f"UPDATE reading_records SET {column}=? WHERE document_id=?",
                               (json.dumps(value, ensure_ascii=False) if key == "tags" else value, str(document_id)))
        row = connection.execute("SELECT * FROM reading_records WHERE document_id=?", (str(document_id),)).fetchone()
    return JSONResponse(_reading(row))


def archive_answer(database: Database, question: str, body: dict[str, object], scope: list[str]) -> str:
    entry_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO research_entries(id,title,question,answer_json,scope_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (entry_id, question.strip()[:120], question.strip(), json.dumps(body, ensure_ascii=False),
             json.dumps(scope), now, now),
        )
    return entry_id


class NewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=50_000)
    note_id: UUID | None = None
    starred: bool = True


class EditNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=50_000)
    revision: int = Field(ge=0)
    starred: bool


def _entry(row: sqlite3.Row, current: dict[str, str | None]) -> dict[str, object]:
    answer = json.loads(row["answer_json"])
    for citation in answer.get("citations", []):
        content_id = citation.get("content_id")
        citation["source_changed"] = not content_id or current.get(citation.get("document_id")) != content_id
    return {"id": row["id"], "title": row["title"], "question": row["question"],
            "answer": answer, "document_ids": json.loads(row["scope_json"]), "note": row["note"],
            "starred": bool(row["starred"]), "revision": row["revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@router.get("/notebook")
def notebook(request: Request) -> JSONResponse:
    with request.app.state.database.connect() as connection:
        rows = connection.execute("SELECT * FROM research_entries ORDER BY created_at DESC,id DESC").fetchall()
        current = {str(row["id"]): row["content_sha256"] for row in connection.execute(
            "SELECT d.id,d.content_sha256 FROM documents d JOIN libraries l ON l.id=d.library_id "
            "WHERE d.status IN ('candidate_pdf','valid_pdf') AND l.status='active'")}
    return JSONResponse({"items": [_entry(row, current) for row in rows]})


@router.get("/notebook/index")
def notebook_index(request: Request, q: str = Query(default="", max_length=2_000),
                   starred: bool = False, offset: int = Query(default=0, ge=0, le=1_000_000),
                   limit: int = Query(default=30, ge=1, le=100)) -> JSONResponse:
    terms = q.casefold().split()
    if len(terms) > 12:
        terms = [q.strip().casefold()]  # Full questions from cached studies remain searchable.
    conditions = ["starred=1"] if starred else []
    params: list[object] = []
    for term in terms:
        conditions.append("""(instr(lw_fold(title),?)>0 OR instr(lw_fold(question),?)>0
            OR instr(lw_fold(note),?)>0 OR instr(lw_fold(json_extract(answer_json,'$.answer')),?)>0
            OR EXISTS (SELECT 1 FROM json_each(answer_json,'$.citations')
                WHERE instr(lw_fold(json_extract(value,'$.excerpt')),?)>0))""")
        params.extend([term] * 5)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with request.app.state.database.connect() as connection:
        connection.create_function('lw_fold', 1, lambda value: str(value or '').casefold(), deterministic=True)
        total = connection.execute("SELECT count(*) FROM research_entries" + where, params).fetchone()[0]
        rows = connection.execute("SELECT id,title,question,starred,revision,created_at,updated_at FROM research_entries" + where +
            " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    workspace_id = hashlib.sha256(str(request.app.state.settings.data_root.resolve()).casefold().encode()).hexdigest()
    return JSONResponse({"items": [{**dict(row), "starred": bool(row['starred'])} for row in rows],
                         "total": total, "offset": offset, "has_more": offset + len(rows) < total,
                         "workspace_id": workspace_id}, headers={"Cache-Control": "no-store"})


@router.get("/notebook/evidence")
def notebook_evidence(request: Request, document_ids: list[UUID] = Query(min_length=1, max_length=8),
                      q: str = Query(default="", max_length=2_000),
                      offset: int = Query(default=0, ge=0, le=1_000_000),
                      limit: int = Query(default=20, ge=1, le=50)) -> JSONResponse:
    from literature_workbench.features.research.evidence_matrix import configure, current_sources, filters, mark_citations

    ids = list(dict.fromkeys(str(item) for item in document_ids))
    where, parameters = filters(ids, q)
    with request.app.state.database.transaction(immediate=False) as connection:
        configure(connection)
        total = connection.execute("SELECT count(*) FROM research_entries" + where, parameters).fetchone()[0]
        rows = connection.execute("""SELECT id,title,question,note,scope_json,created_at,updated_at,
            json_extract(answer_json,'$.citations') AS citations_json,
            json_extract(answer_json,'$.provider') AS provider, json_extract(answer_json,'$.model') AS model,
            json_extract(answer_json,'$.scope') AS scope FROM research_entries""" + where +
            " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", [*parameters, limit, offset]).fetchall()
        current = current_sources(connection)
        items = []
        for row in rows:
            citations = json.loads(row['citations_json'] or '[]')
            mark_citations(citations, current)
            items.append({**{key: row[key] for key in ('id', 'title', 'question', 'note', 'created_at', 'updated_at', 'provider', 'model', 'scope')},
                          'document_ids': json.loads(row['scope_json']), 'citations': citations})
    return JSONResponse({'items': items, 'total': total, 'offset': offset, 'has_more': offset + len(rows) < total,
                         'document_ids': ids, 'query': q, 'viewed_at': datetime.now(UTC).isoformat()},
                        headers={'Cache-Control': 'no-store'})


@router.get("/notebook/evidence/export")
def export_notebook_evidence(request: Request, document_ids: list[UUID] = Query(min_length=1, max_length=8),
                             q: str = Query(default="", max_length=2_000),
                             file_format: str = Query(default="markdown", alias="format", pattern="^(markdown|json)$")) -> StreamingResponse:
    from literature_workbench.features.research.evidence_matrix import export_chunks

    chunks = export_chunks(request.app.state.database.path, list(dict.fromkeys(str(item) for item in document_ids)), q, file_format, _entry)
    extension = 'json' if file_format == 'json' else 'md'
    return StreamingResponse(chunks, media_type='application/json' if file_format == 'json' else 'text/markdown; charset=utf-8',
        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                 'Content-Disposition': f'attachment; filename="evidence-matrix.{extension}"'},
        background=BackgroundTask(chunks.close))


@router.get("/notebook/{entry_id}")
def notebook_entry(request: Request, entry_id: UUID) -> JSONResponse:
    with request.app.state.database.connect() as connection:
        row = connection.execute("SELECT * FROM research_entries WHERE id=?", (str(entry_id),)).fetchone()
        if row is None:
            return JSONResponse({"code": "note_not_found"}, status_code=404)
        current = {str(item["id"]): item["content_sha256"] for item in connection.execute(
            "SELECT d.id,d.content_sha256 FROM documents d JOIN libraries l ON l.id=d.library_id "
            "WHERE d.status IN ('candidate_pdf','valid_pdf') AND l.status='active'")}
    return JSONResponse(_entry(row, current), headers={"Cache-Control": "no-store"})


@router.post("/notebook", status_code=201)
def new_note(request: Request, payload: NewNote) -> JSONResponse:
    now = datetime.now(UTC).isoformat()
    entry_id = str(payload.note_id or uuid4())
    if not payload.title.strip():
        return JSONResponse({"code": "title_required"}, status_code=422)
    with request.app.state.database.transaction() as connection:
        existing = connection.execute("SELECT * FROM research_entries WHERE id=?", (entry_id,)).fetchone()
        if existing is not None:
            if existing['question'] or existing['title'] != payload.title.strip() or existing['note'] != payload.note or bool(existing['starred']) != payload.starred:
                return JSONResponse({"code": "note_conflict"}, status_code=409)
            return JSONResponse({"id": entry_id, "revision": existing['revision'], "created_at": existing['created_at'], "starred": bool(existing['starred'])}, status_code=200)
        connection.execute("INSERT INTO research_entries(id,title,note,starred,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (entry_id, payload.title.strip(), payload.note, int(payload.starred), now, now))
    return JSONResponse({"id": entry_id, "revision": 0, "created_at": now, "starred": payload.starred}, status_code=201)


@router.post("/notebook/{entry_id}/copy")
def copy_note(request: Request, entry_id: UUID, payload: NewNote) -> JSONResponse:
    if not payload.title.strip():
        return JSONResponse({"code": "title_required"}, status_code=422)
    copied_id = str(payload.note_id or uuid4())
    now = datetime.now(UTC).isoformat()
    with request.app.state.database.transaction() as connection:
        original = connection.execute("SELECT * FROM research_entries WHERE id=?", (str(entry_id),)).fetchone()
        if original is None:
            return JSONResponse({"code": "note_not_found"}, status_code=404)
        answer = json.loads(original['answer_json'])
        answer['copied_from_entry_id'] = str(entry_id)
        encoded = json.dumps(answer, ensure_ascii=False)
        existing = connection.execute("SELECT * FROM research_entries WHERE id=?", (copied_id,)).fetchone()
        if existing is not None:
            if existing['title'] != payload.title.strip() or existing['note'] != payload.note or existing['answer_json'] != encoded or existing['scope_json'] != original['scope_json'] or bool(existing['starred']) != payload.starred:
                return JSONResponse({"code": "note_conflict"}, status_code=409)
        else:
            connection.execute("""INSERT INTO research_entries(id,title,question,answer_json,scope_json,note,starred,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (copied_id, payload.title.strip(), original['question'], encoded, original['scope_json'], payload.note, int(payload.starred), now, now))
    return notebook_entry(request, UUID(copied_id))


@router.patch("/notebook/{entry_id}")
def edit_note(request: Request, entry_id: UUID, payload: EditNote) -> JSONResponse:
    if not payload.title.strip():
        return JSONResponse({"code": "title_required"}, status_code=422)
    with request.app.state.database.transaction() as connection:
        existing = connection.execute("SELECT revision FROM research_entries WHERE id=?", (str(entry_id),)).fetchone()
        if existing is None:
            return JSONResponse({"code": "note_not_found"}, status_code=404)
        if int(existing["revision"]) != payload.revision:
            return JSONResponse({"code": "note_conflict"}, status_code=409)
        connection.execute(
            "UPDATE research_entries SET title=?,note=?,starred=?,revision=revision+1,updated_at=? WHERE id=?",
            (payload.title.strip(), payload.note, int(payload.starred), datetime.now(UTC).isoformat(), str(entry_id)),
        )
    return JSONResponse({"revision": payload.revision + 1})
