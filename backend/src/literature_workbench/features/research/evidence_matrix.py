"""Read-only, scoped browsing and portable exports of saved evidence."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def filters(document_ids: list[str], query: str) -> tuple[str, list[object]]:
    terms = query.casefold().split()
    if len(terms) > 12:
        terms = [query.strip().casefold()]
    placeholders = ",".join("?" for _ in document_ids)
    conditions = [f"""question<>'' AND (
        EXISTS (SELECT 1 FROM json_each(scope_json) WHERE value IN ({placeholders}))
        OR EXISTS (SELECT 1 FROM json_each(answer_json,'$.citations')
                   WHERE json_extract(value,'$.document_id') IN ({placeholders})))"""]
    parameters: list[object] = [*document_ids, *document_ids]
    for term in terms:
        conditions.append("""(instr(lw_fold(title),?)>0 OR instr(lw_fold(question),?)>0
            OR instr(lw_fold(note),?)>0 OR instr(lw_fold(json_extract(answer_json,'$.answer')),?)>0
            OR EXISTS (SELECT 1 FROM json_each(answer_json,'$.citations')
                WHERE instr(lw_fold(json_extract(value,'$.excerpt')),?)>0))""")
        parameters.extend([term] * 5)
    return " WHERE " + " AND ".join(conditions), parameters


def configure(connection: sqlite3.Connection) -> None:
    connection.create_function("lw_fold", 1, lambda value: str(value or "").casefold(), deterministic=True)


def current_sources(connection: sqlite3.Connection) -> dict[str, str | None]:
    return {str(row["id"]): str(row["content_sha256"]) for row in connection.execute(
        "SELECT d.id,d.content_sha256 FROM documents d JOIN libraries l ON l.id=d.library_id "
        "WHERE d.status IN ('candidate_pdf','valid_pdf') AND l.status='active'")}


def mark_citations(citations: list[dict[str, Any]], current: dict[str, str | None]) -> None:
    for citation in citations:
        version = citation.get("content_id")
        citation["source_changed"] = not version or current.get(citation.get("document_id", "")) != version


def markdown(value: object) -> str:
    return re.sub(r"([\\`*_{}\[\]#!|])", r"\\\1", html.escape(str("" if value is None else value), quote=False))


def entry_markdown(entry: dict[str, Any]) -> str:
    answer = entry["answer"]
    citations = answer.get("citations", [])
    sections = [f"## {markdown(entry['title'])}",
        f"记录 ID：{markdown(entry['id'])} · 版本：{markdown(entry['revision'])}\n\n创建：{markdown(entry['created_at'])} · 更新：{markdown(entry['updated_at'])}",
        f"### 原研究问题\n{markdown(entry['question'])}",
        f"原提问范围：{markdown(answer.get('scope', '未记录'))}\n\n原选篇 ID：{markdown(', '.join(entry['document_ids']) or '未记录')}\n\n模型：{markdown(answer.get('provider', '未记录'))} / {markdown(answer.get('model', '未记录'))}",
        f"### AI 原始回答\n{markdown(answer.get('answer', ''))}",
        f"### 我的思考\n{markdown(entry['note'] or '尚未填写')}"]
    if answer.get("copied_from_entry_id"):
        sections.append(f"原始记录 ID：{markdown(answer['copied_from_entry_id'])}")
    if answer.get("scope_documents"):
        sections.append("### 原参与文献\n" + "\n".join(
            f"- {markdown(d.get('display_name'))} · {markdown(d.get('document_id'))} · {'本次回答有引用' if d.get('cited') else '本次回答未引用，不代表论文没有证据'}"
            for d in answer["scope_documents"]))
    if answer.get("reading_coverage"):
        sections.append("### 当时研读范围记录\n仅按保存记录展示，不表示本次重新读全文；解析文字不含图片视觉理解。\n\n" + markdown(json.dumps(answer["reading_coverage"], ensure_ascii=False)))
    if answer.get("selection_context"):
        sections.append("### 当时精读选区\n" + markdown(json.dumps(answer["selection_context"], ensure_ascii=False)))
    sections.append("### 原文依据（含原回答的组外来源）")
    sections.append("当前对照列覆盖：\n" + "\n".join(
        f"- {markdown(item['display_name'])}：{'本次回答有引用' if item['cited'] else '本次回答未引用，不代表论文没有相关证据'}"
        for item in entry['matrix_coverage']))
    if not citations:
        sections.append("本次回答没有原文引用；不代表文献没有相关证据。")
    for citation in citations:
        sections.append(f"{markdown(citation.get('display_name'))} · 第 {markdown(citation.get('physical_page', '未记录'))} 页\n\n"
            f"文献 ID：{markdown(citation.get('document_id'))} · 内容版本：{markdown(citation.get('content_id', '未记录'))}\n\n"
            f"来源状态：{'已变化或不可用，历史证据需重新核对' if citation['source_changed'] else '版本与记录一致；不代表科学判断已验证'}\n\n"
            f"> {markdown(citation.get('excerpt')).replace(chr(10), chr(10) + '> ')}")
    return "\n\n".join(sections) + "\n\n---\n\n"


def export_chunks(database_path: Path, document_ids: list[str], query: str, file_format: str,
                  entry_reader: Callable[[sqlite3.Row, dict[str, str | None]], dict[str, Any]]) -> Generator[str, None, None]:
    # Starlette advances a sync iterator serially, possibly on different pool threads.
    # This connection belongs only to this iterator and is never shared concurrently.
    connection = sqlite3.connect(database_path, check_same_thread=False, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        configure(connection)
        connection.execute("BEGIN")
        where, parameters = filters(document_ids, query)
        total = connection.execute("SELECT count(*) FROM research_entries" + where, parameters).fetchone()[0]
        current = current_sources(connection)
        placeholders = ",".join("?" for _ in document_ids)
        documents = {r["id"]: dict(r) for r in connection.execute(
            f"SELECT id,display_name,content_sha256 FROM documents WHERE id IN ({placeholders})", document_ids)}
        scope = [{"document_id": doc_id, "display_name": documents.get(doc_id, {}).get("display_name", "已不可用文献"),
                  "current_content_id": current.get(doc_id), "available": doc_id in current} for doc_id in document_ids]
        metadata = {"format": "literature-workbench-evidence-matrix", "version": 1,
                    "exported_at": datetime.now(UTC).isoformat(), "query": query, "documents": scope, "total": total,
                    "note": "导出全部匹配的已保存问答；保留原提问范围及组外原文依据。未引用不代表论文没有证据；版本一致不代表AI推论已验证。本次未调用AI。"}
        if file_format == "json":
            yield json.dumps(metadata, ensure_ascii=False)[:-1] + ', "entries": ['
        else:
            yield "# 多文献证据对照档案\n\n" + metadata["note"] + "\n\n"
            yield f"导出时间：{metadata['exported_at']}\n\n检索：{markdown(query or '全部')}\n\n记录数：{total}\n\n当前对照列：\n" + "\n".join(
                f"- {markdown(d['display_name'])} · {d['document_id']} · 当前版本 {d['current_content_id'] or '不可用'}" for d in scope) + "\n\n"
        cursor = connection.execute("SELECT * FROM research_entries" + where + " ORDER BY created_at DESC,id DESC", parameters)
        for index, row in enumerate(cursor):
            entry = entry_reader(row, current)
            cited = {c.get('document_id') for c in entry['answer'].get('citations', [])}
            entry['matrix_coverage'] = [{**d, 'cited': d['document_id'] in cited} for d in scope]
            if file_format == "json":
                yield ("," if index else "") + json.dumps(entry, ensure_ascii=False)
            else:
                yield entry_markdown(entry)
        if file_format == "json":
            yield '], "export_complete": true}'
        else:
            yield "\n\n<!-- literature-workbench-evidence-export-complete -->\n"
    finally:
        connection.rollback()
        connection.close()
