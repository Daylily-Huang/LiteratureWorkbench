from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

router = APIRouter(prefix="/api/workspace", tags=["reading-graph"])
RELATIONS = {"related": "相关研究", "supports": "支持", "contradicts": "存在分歧", "method": "方法对照"}


def _documents(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {str(row["id"]): row for row in connection.execute(
        "SELECT d.id,d.display_name,d.content_sha256,d.path,l.root_path,l.id AS library_id,c.page_count,c.current_parse_run_id "
        "FROM documents d JOIN libraries l ON l.id=d.library_id "
        "JOIN contents c ON c.sha256=d.content_sha256 "
        "WHERE l.status='active' AND d.status IN ('candidate_pdf','valid_pdf') AND d.format='pdf'"
    )}


class GroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    document_ids: list[UUID] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def normalize(self) -> GroupInput:
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("title_required")
        self.document_ids = list(dict.fromkeys(self.document_ids))
        return self


class GroupEdit(GroupInput):
    revision: int = Field(ge=0)


@router.get("/groups")
def groups(request: Request) -> JSONResponse:
    with request.app.state.database.connect() as connection:
        rows = connection.execute("SELECT * FROM reading_groups ORDER BY updated_at DESC,id").fetchall()
    return JSONResponse({"items": [{"id": r["id"], "title": r["title"], "document_ids": json.loads(r["document_ids_json"]),
                                   "revision": r["revision"]} for r in rows]})


def _save_group(request: Request, payload: GroupInput, group_id: str | None) -> JSONResponse:
    ids = [str(item) for item in payload.document_ids]
    now = datetime.now(UTC).isoformat()
    with request.app.state.database.transaction() as connection:
        available = _documents(connection)
        if any(item not in available for item in ids):
            return JSONResponse({"code": "document_not_found"}, status_code=404)
        if group_id is None:
            group_id = str(uuid4())
            connection.execute("INSERT INTO reading_groups(id,title,document_ids_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (group_id, payload.title, json.dumps(ids), now, now))
            revision = 0
        else:
            row = connection.execute("SELECT revision FROM reading_groups WHERE id=?", (group_id,)).fetchone()
            if row is None:
                return JSONResponse({"code": "group_not_found"}, status_code=404)
            if not isinstance(payload, GroupEdit) or row["revision"] != payload.revision:
                return JSONResponse({"code": "group_conflict"}, status_code=409)
            revision = payload.revision + 1
            connection.execute("UPDATE reading_groups SET title=?,document_ids_json=?,revision=?,updated_at=? WHERE id=?",
                               (payload.title, json.dumps(ids), revision, now, group_id))
    return JSONResponse({"id": group_id, "revision": revision})


@router.post("/groups")
def create_group(request: Request, payload: GroupInput) -> JSONResponse:
    return _save_group(request, payload, None)


@router.patch("/groups/{group_id}")
def update_group(request: Request, group_id: UUID, payload: GroupEdit) -> JSONResponse:
    return _save_group(request, payload, str(group_id))


@router.delete("/groups/{group_id}")
def delete_group(request: Request, group_id: UUID) -> JSONResponse:
    with request.app.state.database.transaction() as connection:
        connection.execute("DELETE FROM reading_groups WHERE id=?", (str(group_id),))
    return JSONResponse({"deleted": True})


class RelationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    target_id: UUID
    relation: Literal["related", "supports", "contradicts", "method"]
    note: str = Field(min_length=1, max_length=4000)
    source_page: int = Field(ge=1, le=100000)
    target_page: int = Field(ge=1, le=100000)
    source_content_id: str = Field(min_length=64, max_length=64)
    target_content_id: str = Field(min_length=64, max_length=64)
    question: str = Field(default="", max_length=500)
    difference_basis: Literal["unspecified", "claim", "method", "sample", "context"] = "unspecified"
    comparison_note: str = Field(default="", max_length=4000)


@router.post("/relations")
def create_relation(request: Request, payload: RelationInput) -> JSONResponse:
    if payload.source_id == payload.target_id or not payload.note.strip():
        return JSONResponse({"code": "invalid_relation"}, status_code=422)
    with request.app.state.database.transaction() as connection:
        docs = _documents(connection)
        for doc_id, page, version in ((str(payload.source_id), payload.source_page, payload.source_content_id),
                                      (str(payload.target_id), payload.target_page, payload.target_content_id)):
            doc = docs.get(doc_id)
            if doc is None:
                return JSONResponse({"code": "document_not_found"}, status_code=404)
            if doc["content_sha256"] != version:
                return JSONResponse({"code": "source_changed"}, status_code=409)
            if doc["page_count"] is None or page > doc["page_count"]:
                return JSONResponse({"code": "page_unavailable"}, status_code=409)
        relation_id = str(uuid4())
        connection.execute(
            "INSERT INTO document_relations(id,source_id,target_id,relation,note,source_page,target_page,"
            "source_content_id,target_content_id,created_at,question,difference_basis,comparison_note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (relation_id, str(payload.source_id), str(payload.target_id), payload.relation, payload.note.strip(),
             payload.source_page, payload.target_page, payload.source_content_id, payload.target_content_id,
             datetime.now(UTC).isoformat(), payload.question.strip(), payload.difference_basis, payload.comparison_note.strip()),
        )
    return JSONResponse({"id": relation_id})


@router.delete("/relations/{relation_id}")
def delete_relation(request: Request, relation_id: UUID) -> JSONResponse:
    with request.app.state.database.transaction() as connection:
        connection.execute("DELETE FROM document_relations WHERE id=?", (str(relation_id),))
    return JSONResponse({"deleted": True})


@router.get("/graph")
def graph(request: Request) -> JSONResponse:
    from pathlib import Path

    nodes: dict[str, dict[str, object]] = {}
    edges: list[dict[str, object]] = []
    with request.app.state.database.connect() as connection:
        docs = _documents(connection)
        def document_node(doc_id: str, fallback: str = "原文献") -> str:
            node_id = "doc:" + doc_id
            if node_id not in nodes:
                row = docs.get(doc_id)
                nodes[node_id] = {"id": node_id, "kind": "document", "label": row["display_name"] if row else fallback,
                                  "document_id": doc_id, "available": row is not None}
            return node_id

        for doc_id, doc in docs.items():
            node_id = document_node(doc_id)
            try:
                folder = str(Path(doc["path"]).parent.relative_to(Path(doc["root_path"])))
            except ValueError:
                folder = "."
            if folder != ".":
                folder_id = f"folder:{doc['library_id']}:{folder}"
                nodes[folder_id] = {"id": folder_id, "kind": "folder", "label": folder}
                edges.append({"id": f"folder:{doc_id}", "source": node_id, "target": folder_id, "kind": "folder", "label": "所在目录"})
        for row in connection.execute("SELECT document_id,tags_json FROM reading_records"):
            if row["document_id"] not in docs:
                continue
            for tag in json.loads(row["tags_json"]):
                tag_id = "topic:" + tag
                nodes[tag_id] = {"id": tag_id, "kind": "topic", "label": tag}
                edges.append({"id": f"tag:{row['document_id']}:{tag}", "source": "doc:" + row["document_id"], "target": tag_id, "kind": "tag", "label": "主题标签"})
        rows = connection.execute("SELECT id,title,answer_json FROM research_entries WHERE question<>'' ORDER BY created_at DESC,id DESC LIMIT 201").fetchall()
        truncated = len(rows) > 200
        for row in rows[:200]:
            answer = json.loads(row["answer_json"])
            for i, citation in enumerate(answer.get("citations", [])):
                doc_id = citation.get("document_id")
                if not doc_id:
                    continue
                question_id = "question:" + row["id"]
                nodes[question_id] = {"id": question_id, "kind": "question", "label": row["title"], "entry_id": row["id"]}
                target = document_node(doc_id, citation.get("display_name", "原文献"))
                changed = doc_id not in docs or not citation.get("content_id") or docs[doc_id]["content_sha256"] != citation["content_id"]
                edges.append({"id": f"citation:{row['id']}:{i}", "source": question_id, "target": target, "kind": "citation", "label": "回答引用", "source_changed": changed, "citation": citation})
        for row in connection.execute("SELECT * FROM document_relations ORDER BY created_at DESC,id"):
            from literature_workbench.features.research.study_relations import study_context_changed
            evidence = json.loads(row['evidence_json'])
            changed = any(doc_id not in docs or docs[doc_id]["content_sha256"] != version for doc_id, version in
                          ((row["source_id"], row["source_content_id"]), (row["target_id"], row["target_content_id"])))
            changed = changed or study_context_changed(evidence.get('ai_proposal', {}), docs)
            edges.append({"id": "manual:" + row["id"], "relation_id": row["id"], "source": document_node(row["source_id"]),
                          "target": document_node(row["target_id"]), "kind": "manual", "label": RELATIONS[row["relation"]],
                          "relation": row["relation"], "note": row["note"], "question": row["question"],
                          "origin": row["origin"], "evidence": evidence,
                          "difference_basis": row["difference_basis"], "comparison_note": row["comparison_note"],
                          "source_page": row["source_page"], "target_page": row["target_page"],
                          "source_content_id": row["source_content_id"], "target_content_id": row["target_content_id"], "source_changed": changed})
            if row["question"]:
                key = hashlib.sha256(" ".join(row["question"].split()).casefold().encode()).hexdigest()
                question_id = "evidence-question:" + key
                nodes[question_id] = {"id": question_id, "kind": "question", "label": row["question"], "question_text": row["question"]}
                for doc_id in (row["source_id"], row["target_id"]):
                    edges.append({"id": f"question-link:{row['id']}:{doc_id}", "source": question_id,
                                  "target": document_node(doc_id), "kind": "question_link", "label": "人工证据对照",
                                  "source_changed": changed})
        for row in connection.execute("SELECT * FROM reading_groups ORDER BY updated_at DESC,id"):
            group_id = "group:" + row["id"]
            nodes[group_id] = {"id": group_id, "kind": "group", "label": row["title"], "group_id": row["id"]}
            for doc_id in json.loads(row["document_ids_json"]):
                edges.append({"id": group_id + ":" + doc_id, "source": document_node(doc_id), "target": group_id, "kind": "group", "label": "研读组成员"})
    return JSONResponse({"nodes": list(nodes.values()), "edges": edges, "history_truncated": truncated})
