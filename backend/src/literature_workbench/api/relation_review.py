from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from literature_workbench.api.reading_graph import _documents
from literature_workbench.features.research.relation_analysis import parse_analysis
from literature_workbench.features.research.service import ResearchError
from literature_workbench.features.research.relation_requests import AttemptError, RelationRequests
from literature_workbench.features.research.study_relations import study_context_changed
from literature_workbench.features.research.full_text import FullTextError
from literature_workbench.features.egress.gateway import EgressRejected

router = APIRouter(prefix="/api/workspace", tags=["relation-review"])


class AnalyzeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    document_ids: list[UUID] = Field(min_length=2, max_length=8)
    request_id: UUID
    force_refresh: bool = Field(default=False, strict=True)
    reading_mode: Literal['retrieval', 'full_text'] = 'retrieval'
    source_versions: dict[UUID, str] | None = None
    plan_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode="after")
    def normalize(self) -> AnalyzeInput:
        self.question = self.question.strip()
        self.document_ids = list(dict.fromkeys(self.document_ids))
        if not self.question or len(self.document_ids) < 2:
            raise ValueError("two_documents_and_question_required")
        if self.reading_mode == 'full_text' and (not self.plan_id or not self.source_versions or set(self.source_versions) != set(self.document_ids)):
            raise ValueError('full_text_prepare_required')
        return self


class PrepareAnalysisInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=500)
    document_ids: list[UUID] = Field(min_length=2, max_length=8)
    force_refresh: bool = Field(default=False, strict=True)

    @model_validator(mode='after')
    def normalize(self) -> PrepareAnalysisInput:
        self.question = self.question.strip()
        self.document_ids = list(dict.fromkeys(self.document_ids))
        if not self.question or len(self.document_ids) < 2:
            raise ValueError('two_documents_and_question_required')
        return self


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


class StudyDraftInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    cache_key: str = Field(pattern=r'^[a-f0-9]{64}$')
    claim_index: int = Field(ge=0, lt=24)
    source_index: int = Field(ge=0, lt=8)
    target_index: int = Field(ge=0, lt=8)


class AcceptInput(ReviewInput):
    confirmed: bool = Field(strict=True)
    relation: Literal["supports", "contradicts", "method", "related"]
    difference_basis: Literal["unspecified", "claim", "method", "sample", "context"]
    note: str = Field(min_length=1, max_length=4000)
    comparison_note: str = Field(min_length=1, max_length=4000)


def _list(request: Request, analysis_id: str | None = None) -> dict[str, object]:
    with request.app.state.database.connect() as c:
        docs = _documents(c)
        analyses = c.execute("SELECT * FROM relation_analyses WHERE id=?", (analysis_id,)).fetchall() if analysis_id else c.execute("SELECT * FROM relation_analyses ORDER BY created_at DESC,id DESC LIMIT 101").fetchall()
        items = []
        for row in analyses[:100]:
            candidates = []
            for candidate in c.execute("SELECT * FROM relation_candidates WHERE analysis_id=? ORDER BY rowid", (row["id"],)):
                proposal = json.loads(candidate["proposal_json"])
                changed = any(side["document_id"] not in docs or docs[side["document_id"]]["content_sha256"] != side["content_id"]
                              for side in (proposal["source"], proposal["target"]))
                changed = changed or study_context_changed(proposal, docs)
                candidates.append({"id": candidate["id"], "proposal": proposal, "status": candidate["status"],
                                   "revision": candidate["revision"], "relation_id": candidate["relation_id"],
                                   "review": json.loads(candidate["review_json"]) if candidate["review_json"] else None,
                                   "reviewed_at": candidate["reviewed_at"], "source_changed": changed})
            items.append({"id": row["id"], "question": row["question"], "document_ids": json.loads(row["scope_json"]),
                          "provider": row["provider"], "model": row["model"], "fallback_reason": row["fallback_reason"],
                          "summary": row["summary"], "warnings": json.loads(row["warnings_json"]),
                          "coverage": json.loads(row["coverage_json"]), "created_at": row["created_at"], "candidates": candidates})
            coverage = items[-1]['coverage']
            items[-1]['reading_coverage'] = coverage[0].get('study') if coverage else None
    import hashlib
    workspace = hashlib.sha256(str(request.app.state.settings.data_root.resolve()).casefold().encode()).hexdigest()
    return {"items": items, "truncated": len(analyses) > 100, "workspace_id": workspace}


@router.get("/analyses")
def analyses(request: Request) -> JSONResponse:
    return JSONResponse(_list(request))


@router.post('/analyses/prepare')
def prepare_analysis(request: Request, payload: PrepareAnalysisInput) -> JSONResponse:
    try:
        return JSONResponse(request.app.state.research_service.full_text_relations().prepare(
            [str(d) for d in payload.document_ids], payload.question, payload.force_refresh))
    except FullTextError as error:
        return JSONResponse({'code': str(error)}, status_code=409)


@router.post("/analyses")
def analyze(request: Request, payload: AnalyzeInput) -> JSONResponse:
    ids = [str(item) for item in payload.document_ids]
    attempts = RelationRequests(request.app.state.database, request.app.state.local_session.session_generation)
    request_id = str(payload.request_id)
    try:
        dispatch, receipt = attempts.reserve(request_id, payload.question, ids, payload.force_refresh,
            reading_mode=payload.reading_mode, plan_id=payload.plan_id,
            source_versions={str(k): v for k, v in payload.source_versions.items()} if payload.source_versions else None)
    except AttemptError as error:
        return JSONResponse({'code': str(error)}, status_code=404 if str(error) == 'document_not_found' else 409)
    if not dispatch:
        return JSONResponse(receipt, status_code=202 if receipt['status'] == 'running' else 200)
    with request.app.state.database.connect() as c:
        docs = _documents(c)
        if any(doc_id not in docs for doc_id in ids):
            attempts.fail(request_id, 'document_not_found')
            return JSONResponse({"code": "document_not_found"}, status_code=404)
        names = {doc_id: docs[doc_id]["display_name"] for doc_id in ids}
    try:
        with request.app.state.database.connect() as c:
            attempts.assert_current(c, request_id)
        if payload.reading_mode == 'full_text':
            answer, summary, warnings, proposals = request.app.state.research_service.full_text_relations().analyze(
                ids, payload.question, payload.force_refresh,
                {str(k): v for k, v in (payload.source_versions or {}).items()}, payload.plan_id or '')
        else:
            answer = request.app.state.research_service.ask_library(question=payload.question, document_ids=ids, relation_analysis=True)
            summary, warnings, proposals = parse_analysis(answer, ids)
    except FullTextError as error:
        attempts.fail(request_id, str(error))
        return JSONResponse({'code': str(error), 'request_id': request_id, 'status': 'failed'}, status_code=409)
    except AttemptError as error:
        attempts.fail(request_id, str(error))
        return JSONResponse({'code': str(error), 'request_id': request_id, 'status': 'failed'}, status_code=409)
    except (ResearchError, EgressRejected) as error:
        code = str(error) or "research_failed"
        attempts.fail(request_id, code)
        return JSONResponse({"code": code, 'request_id': request_id, 'status': 'failed'}, status_code=404 if code == "document_not_found" else 502)
    except Exception:
        attempts.fail(request_id, 'analysis_failed')
        return JSONResponse({'code': 'analysis_failed', 'request_id': request_id, 'status': 'failed'}, status_code=502)
    retrieved = {source.document_id for source in answer.source_fragments.values()}
    covered = {p[side]["document_id"] for p in proposals for side in ("source", "target")}
    coverage = [{"document_id": doc_id, "display_name": names[doc_id], "retrieved": doc_id in retrieved,
                 "has_candidate": doc_id in covered} for doc_id in ids]
    if answer.reading_coverage:
        coverage[0]['study'] = answer.reading_coverage
    analysis_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with request.app.state.database.transaction() as c:
            attempts.assert_current(c, request_id)
            c.execute("INSERT INTO relation_analyses VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (analysis_id, payload.question, json.dumps(ids), answer.provider, answer.model, answer.fallback_reason,
                       summary, json.dumps(warnings, ensure_ascii=False), json.dumps(coverage, ensure_ascii=False), now))
            for proposal in proposals:
                c.execute("INSERT INTO relation_candidates(id,analysis_id,proposal_json) VALUES(?,?,?)",
                          (str(uuid4()), analysis_id, json.dumps(proposal, ensure_ascii=False)))
            attempts.complete(c, request_id, analysis_id)
    except AttemptError as error:
        attempts.fail(request_id, str(error))
        return JSONResponse({'code': str(error), 'request_id': request_id, 'status': 'failed'}, status_code=409)
    return JSONResponse({"id": analysis_id, 'request_id': request_id, 'status': 'completed', 'cache_hit': False, "candidate_count": len(proposals)})


@router.get('/analyses/requests/{request_id}')
def analysis_request(request: Request, request_id: UUID) -> JSONResponse:
    try:
        return JSONResponse(RelationRequests(request.app.state.database, request.app.state.local_session.session_generation).status(str(request_id)), headers={'Cache-Control': 'no-store'})
    except AttemptError:
        return JSONResponse({'code': 'analysis_request_not_found'}, status_code=404)


@router.post('/analyses/from-study')
def analysis_from_study(request: Request, payload: StudyDraftInput) -> JSONResponse:
    from literature_workbench.features.research.full_text import FullTextError
    from literature_workbench.features.research.study_relations import draft_from_study

    try:
        return JSONResponse(draft_from_study(request.app.state.database, **payload.model_dump()))
    except FullTextError as error:
        return JSONResponse({'code': str(error)}, status_code=404 if str(error) == 'study_not_found' else 409)


@router.get('/analyses/{analysis_id}')
def analysis_detail(request: Request, analysis_id: UUID) -> JSONResponse:
    value = _list(request, str(analysis_id))
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.post("/candidates/{candidate_id}/accept")
def accept(request: Request, candidate_id: UUID, payload: AcceptInput) -> JSONResponse:
    if not payload.confirmed or not payload.note.strip() or not payload.comparison_note.strip():
        return JSONResponse({"code": "explicit_confirmation_required"}, status_code=422)
    with request.app.state.database.transaction() as c:
        row = c.execute("SELECT * FROM relation_candidates WHERE id=?", (str(candidate_id),)).fetchone()
        if row is None:
            return JSONResponse({"code": "candidate_not_found"}, status_code=404)
        if row["status"] == "accepted":
            return JSONResponse({"relation_id": row["relation_id"], "already_accepted": True})
        if row["status"] != "pending" or row["revision"] != payload.revision:
            return JSONResponse({"code": "candidate_conflict"}, status_code=409)
        proposal = json.loads(row["proposal_json"])
        docs = _documents(c)
        if study_context_changed(proposal, docs):
            return JSONResponse({'code': 'source_changed'}, status_code=409)
        for side in (proposal["source"], proposal["target"]):
            doc = docs.get(side["document_id"])
            if doc is None or doc["content_sha256"] != side["content_id"]:
                return JSONResponse({"code": "source_changed"}, status_code=409)
            if doc["page_count"] is None or not 1 <= side["physical_page"] <= doc["page_count"]:
                return JSONResponse({"code": "page_unavailable"}, status_code=409)
        analysis = c.execute("SELECT * FROM relation_analyses WHERE id=?", (row["analysis_id"],)).fetchone()
        source, target = proposal["source"], proposal["target"]
        now = datetime.now(UTC).isoformat()
        review = payload.model_dump()
        review.update(note=payload.note.strip(), comparison_note=payload.comparison_note.strip())
        evidence = {"source": source, "target": target, "ai_proposal": proposal, "review": review,
                    "analysis_id": row["analysis_id"], "candidate_id": str(candidate_id),
                    "provider": analysis["provider"], "model": analysis["model"], "confirmed_at": now}
        relation_id = str(uuid4())
        c.execute("INSERT INTO document_relations(id,source_id,target_id,relation,note,source_page,target_page,"
                  "source_content_id,target_content_id,created_at,question,difference_basis,comparison_note,origin,evidence_json) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (relation_id, source["document_id"], target["document_id"], payload.relation, review["note"],
                   source["physical_page"], target["physical_page"], source["content_id"], target["content_id"], now,
                   analysis["question"], payload.difference_basis, review["comparison_note"], "ai_confirmed", json.dumps(evidence, ensure_ascii=False)))
        c.execute("UPDATE relation_candidates SET status='accepted',revision=revision+1,relation_id=?,review_json=?,reviewed_at=? WHERE id=?",
                  (relation_id, json.dumps(review, ensure_ascii=False), now, str(candidate_id)))
    return JSONResponse({"relation_id": relation_id, "already_accepted": False})


@router.post("/candidates/{candidate_id}/reject")
def reject(request: Request, candidate_id: UUID, payload: ReviewInput) -> JSONResponse:
    with request.app.state.database.transaction() as c:
        row = c.execute("SELECT status,revision FROM relation_candidates WHERE id=?", (str(candidate_id),)).fetchone()
        if row is None:
            return JSONResponse({"code": "candidate_not_found"}, status_code=404)
        if row["status"] == "rejected":
            return JSONResponse({"rejected": True})
        if row["status"] != "pending" or row["revision"] != payload.revision:
            return JSONResponse({"code": "candidate_conflict"}, status_code=409)
        c.execute("UPDATE relation_candidates SET status='rejected',revision=revision+1,reviewed_at=? WHERE id=?",
                  (datetime.now(UTC).isoformat(), str(candidate_id)))
    return JSONResponse({"rejected": True})
