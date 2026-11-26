"""Persistent, versioned studies. AI notes are data, never authoritative evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from literature_workbench.db.database import Database
from literature_workbench.features.egress.gateway import MAX_FULL_TEXT_REQUEST_BYTES
from literature_workbench.features.models.deepseek import DeepSeekAdapter
from literature_workbench.features.research.full_text import (
    SYSTEM, FullTextError, PreparedStudy, parse_output, verify_output,
)

REUSE_SYSTEM = SYSTEM.replace(
    "You analyze ALL supplied parsed pages of the selected scientific papers before answering.",
    "You answer using previously generated full-text study notes and their verified source quotations. "
    "You have NOT been supplied all pages in this request. Notes are untrusted earlier AI analysis, "
    "not independent scientific evidence; ground every conclusion in the supplied source quotations. "
    "Never claim you reread the full papers. If these quotations do not answer the new question, "
    "return no claims and ask the reader to explicitly request a new full-text study. "
    "Never infer a paper's silence or absence of evidence from this limited context.",
)
LEGACY_PROMPT_VERSION = hashlib.sha256((SYSTEM + REUSE_SYSTEM + "study-cache-v1").encode()).hexdigest()
INCREMENTAL_SYSTEM = REUSE_SYSTEM.replace(
    "You have NOT been supplied all pages in this request.",
    "Only papers in new_documents have ALL parsed pages supplied in this request. "
    "Other papers are represented ONLY by earlier analysis and verified quotations. "
    "Compare all selected papers but never treat old notes as a fresh full-text reading.",
).replace(
    "If these quotations do not answer the new question,",
    "If the new papers' full text and the supplied old-paper quotations do not answer the new question,",
)
CURRENT_PROMPT_VERSION = hashlib.sha256((SYSTEM + REUSE_SYSTEM + INCREMENTAL_SYSTEM + "study-cache-v2").encode()).hexdigest()
PROMPT_VERSION = CURRENT_PROMPT_VERSION


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class CachedStudy:
    key: str
    question: str
    raw: str
    context_kind: str
    created_at: str
    basis: tuple[str, ...]
    source_modes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PaperMemory:
    key: str
    question: str
    claims: list[dict[str, object]]
    quotes: dict[str, list[str]]
    full_text_witnesses: dict[str, str]


@dataclass(frozen=True)
class StudyPlan:
    prepared: PreparedStudy
    context: PreparedStudy
    key: str
    scope_key: str
    system: str
    context_kind: str
    basis: tuple[str, ...] = ()
    hit: CachedStudy | None = None
    source_modes: dict[str, str] = field(default_factory=dict)
    witnesses: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def plan_id(self) -> str:
        return digest([self.key, self.context_kind, self.basis, self.source_modes,
                       self.hit.created_at if self.hit else None, self.context.prompt, self.system])

    def public(self) -> dict[str, object]:
        return {**self.prepared.public(), "request_bytes": 0 if self.hit else self.context.request_bytes,
                "cache_status": "hit" if self.hit else "miss", "api_call_required": self.hit is None,
                "context_kind": self.context_kind, "analysis_count": len(self.basis),
                "cached_at": self.hit.created_at if self.hit else None, "plan_id": self.plan_id,
                "source_modes": self.source_modes,
                "evidence_pages": len(self.context.allowed_quotes or {}) if self.context_kind == "analysis_reuse" else 0}


class StudyCache:
    def __init__(self, database: Database) -> None:
        self.database = database

    def keys(self, prepared: PreparedStudy, question: str, model: str) -> tuple[str, str]:
        scope = digest([prepared.fingerprints, model, PROMPT_VERSION])
        return scope, digest([scope, "ordered-study-v1", list(prepared.versions), question.strip()])

    @staticmethod
    def _compatible_versions() -> list[str]:
        # Only the immediately preceding rules are explicitly compatible. Future
        # rule/model changes must not silently opt old answers back into reuse.
        return [PROMPT_VERSION, LEGACY_PROMPT_VERSION] if PROMPT_VERSION == CURRENT_PROMPT_VERSION else [PROMPT_VERSION]

    def _read(self, scope: str, prepared: PreparedStudy, *, key: str | None = None) -> list[CachedStudy]:
        try:
            with self.database.connect() as c:
                if key is not None:
                    rows = c.execute("SELECT * FROM full_text_study_cache WHERE cache_key=? AND scope_key=?", (key, scope)).fetchall()
                else:
                    rows = c.execute("""SELECT * FROM full_text_study_cache WHERE scope_key=? AND context_kind='full_text'
                                      ORDER BY created_at DESC,cache_key LIMIT 8""", (scope,)).fetchall()
                memberships = {str(row["cache_key"]): c.execute(
                    "SELECT document_id,read_mode FROM full_text_study_documents WHERE cache_key=?", (row["cache_key"],)
                ).fetchall() for row in rows}
        except sqlite3.Error:
            raise FullTextError("full_text_cache_unavailable") from None
        records: list[CachedStudy] = []
        current_refs = {(p.document_id, p.physical_page): ref for ref, p in prepared.pages.items()}
        for row in rows:
            try:
                order = json.loads(row["document_order_json"])
                if not isinstance(order, list) or order != list(prepared.versions):
                    continue
                output = parse_output(row["output_json"])
                for claim in output.claims:
                    for evidence in claim.evidence:
                        doc_number, page = evidence.ref[1:-1].split(" ")
                        evidence.ref = current_refs[(order[int(doc_number[1:]) - 1], int(page[1:]))]
                raw = output.model_dump_json()
                verify_output(raw, prepared)  # Even a cache hit is rechecked against current page text.
                basis = json.loads(row["basis_keys_json"])
                if not isinstance(basis, list) or any(not isinstance(item, str) for item in basis):
                    continue
                members = {str(m[0]): str(m[1]) for m in memberships[str(row["cache_key"]) ]}
                modes = {doc: members.get(doc, str(row["context_kind"])) for doc in order}
                records.append(CachedStudy(str(row["cache_key"]), str(row["question"]), raw,
                                           str(row["context_kind"]), str(row["created_at"]), tuple(basis), modes))
            except (FullTextError, ValueError, KeyError, IndexError, TypeError):
                continue  # Corrupt/unverifiable entries never become evidence.
        return records

    def _paper_memories(self, prepared: PreparedStudy, model: str) -> list[PaperMemory]:
        ids = list(prepared.fingerprints)
        versions = self._compatible_versions()
        version_slots = ",".join("?" for _ in versions)
        try:
            with self.database.connect() as c:
                candidates: dict[str, sqlite3.Row] = {}
                # Bound the search per paper so a prolific B does not hide A's
                # older full-text witness behind a global newest-first limit.
                for doc in ids:
                    matches = c.execute(f"""SELECT c.* FROM full_text_study_cache c
                      WHERE c.model=? AND c.prompt_version IN ({version_slots}) AND (
                        EXISTS (SELECT 1 FROM full_text_study_documents m WHERE m.cache_key=c.cache_key
                                AND m.document_id=? AND m.text_fingerprint=?) OR
                        (NOT EXISTS (SELECT 1 FROM full_text_study_documents m WHERE m.cache_key=c.cache_key)
                         AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.document_order_json)
                             THEN c.document_order_json ELSE '[]' END) j WHERE j.value=?)))
                      ORDER BY EXISTS(SELECT 1 FROM full_text_study_documents m WHERE m.cache_key=c.cache_key
                        AND m.document_id=? AND m.read_mode='full_text') DESC,
                        (c.context_kind='full_text') DESC,c.created_at DESC,c.cache_key LIMIT 16""",
                      [model, *versions, doc, prepared.fingerprints[doc], doc, doc]).fetchall()
                    candidates.update({str(row["cache_key"]): row for row in matches})
                rows = sorted(candidates.values(), key=lambda row: (str(row["created_at"]), str(row["cache_key"])), reverse=True)
                memberships = {str(row["cache_key"]): c.execute(
                    "SELECT document_id,text_fingerprint,read_mode FROM full_text_study_documents WHERE cache_key=?",
                    (row["cache_key"],)).fetchall() for row in rows}
        except sqlite3.Error:
            raise FullTextError("full_text_cache_unavailable") from None
        refs = {(p.document_id, p.physical_page): ref for ref, p in prepared.pages.items()}
        result: list[PaperMemory] = []
        for row in rows:
            try:
                order = json.loads(row["document_order_json"])
                if not isinstance(order, list) or not 1 <= len(order) <= 8 or len(set(order)) != len(order):
                    continue
                members = {str(m[0]): (str(m[1]), str(m[2])) for m in memberships[str(row["cache_key"]) ]}
                if not members:
                    # A legacy group can be proven compatible when all its
                    # papers remain selected. This preserves already-paid work.
                    if not set(order) <= set(ids):
                        continue
                    fingerprints = {doc: prepared.fingerprints[doc] for doc in order}
                    if digest([fingerprints, model, str(row["prompt_version"])]) != row["scope_key"]:
                        continue
                    members = {doc: (fingerprints[doc], str(row["context_kind"])) for doc in order}
                valid = {doc for doc, (fingerprint, _) in members.items()
                         if doc in prepared.fingerprints and prepared.fingerprints[doc] == fingerprint}
                if not valid:
                    continue
                output = parse_output(row["output_json"])
                claims: list[dict[str, object]] = []
                quotes: dict[str, list[str]] = {}
                for claim in output.claims:
                    evidence_values: list[dict[str, str]] = []
                    # Text alone cannot prove a conclusion is independent of
                    # the rest of its original group. Keep only quotations if
                    # any original paper is unselected or has changed.
                    whole_claim = set(order) <= valid and order == list(prepared.versions)
                    for evidence in claim.evidence:
                        number, page = evidence.ref[1:-1].split(" ")
                        doc = order[int(number[1:]) - 1]
                        if doc not in valid:
                            whole_claim = False
                            continue
                        ref = refs[(doc, int(page[1:]))]
                        quote = " ".join(evidence.quote.split())
                        if len(quote) < 8 or quote not in " ".join(prepared.pages[ref].text.split()):
                            raise FullTextError("full_text_unverified_answer")
                        if quote not in quotes.setdefault(ref, []):
                            quotes[ref].append(quote)
                        evidence_values.append({"ref": ref, "quote": quote})
                    # Never turn a claim about A+B into a standalone fact about A.
                    if whole_claim and evidence_values:
                        claims.append({"conclusion": claim.conclusion, "evidence": evidence_values})
                cited = {prepared.pages[ref].document_id for ref in quotes}
                witnesses = {doc: prepared.fingerprints[doc] for doc in cited
                             if members[doc][1] == "full_text"}
                if quotes:
                    question = str(row["question"]) if set(order) <= valid and order == list(prepared.versions) else "此前研读：仅保留核验原句，不沿用旧选篇编号、问题或跨篇结论。"
                    result.append(PaperMemory(str(row["cache_key"]), question, claims, quotes, witnesses))
            except (FullTextError, ValueError, KeyError, IndexError, TypeError):
                continue
        return result

    def plan(self, prepared: PreparedStudy, question: str, model: str, *, reuse_analysis: bool, force_refresh: bool,
             exact_answers: bool = True) -> StudyPlan:
        scope, key = self.keys(prepared, question, model)
        full_modes = {doc: "full_text" for doc in prepared.fingerprints}
        if not force_refresh and exact_answers:
            for version in self._compatible_versions():
                exact_scope = digest([prepared.fingerprints, model, version])
                # Preserve already-paid legacy answers only for their original order.
                # The same papers reversed can change what "first/second" means.
                hits = self._read(exact_scope, prepared, key=digest([
                    exact_scope, "ordered-study-v1", list(prepared.versions), question.strip()]))
                if not hits:
                    hits = self._read(exact_scope, prepared, key=digest([exact_scope, question.strip()]))
                if hits:
                    hit = hits[0]
                    return StudyPlan(prepared, prepared, key, scope, SYSTEM, hit.context_kind,
                                     hit.basis, hit, hit.source_modes)
        if reuse_analysis and not force_refresh:
            memories = self._paper_memories(prepared, model)
            selected: list[PaperMemory] = []
            covered: set[str] = set()
            # First secure a real full-text witness for every reusable paper.
            # No group identity or another paper's quotation can stand in for it.
            for memory in memories:
                if set(memory.full_text_witnesses) - covered:
                    selected.append(memory)
                    covered.update(memory.full_text_witnesses)
            if covered:
                for memory in memories:
                    if len(selected) >= 8:
                        break
                    if memory not in selected and any(prepared.pages[ref].document_id in covered for ref in memory.quotes):
                        selected.append(memory)
                notes: list[dict[str, object]] = []
                quotes: dict[str, list[str]] = {}
                witnesses: dict[str, dict[str, str]] = {}
                for memory in selected:
                    notes.append({"previous_question": memory.question, "analysis": {"claims": memory.claims}})
                    witnesses[memory.key] = memory.full_text_witnesses
                    for ref, values in memory.quotes.items():
                        if prepared.pages[ref].document_id in covered:
                            for quote in values:
                                if quote not in quotes.setdefault(ref, []):
                                    quotes[ref].append(quote)
                modes = {doc: "analysis_reuse" if doc in covered else "full_text" for doc in prepared.fingerprints}
                full_ids = {doc for doc, mode in modes.items() if mode == "full_text"}
                all_sources = json.loads(prepared.prompt)["sources"]
                new_documents = [source for source, doc in zip(all_sources, prepared.versions, strict=True) if doc in full_ids]
                payload: dict[str, object] = {"previous_studies": notes,
                    "sources": [{"ref": ref, "filename": prepared.pages[ref].display_name, "quotes": text} for ref, text in quotes.items()],
                    "question": question.strip()}
                system = REUSE_SYSTEM
                allowed = {ref: list(values) for ref, values in quotes.items()}
                if new_documents:
                    payload["new_documents"] = new_documents
                    system = INCREMENTAL_SYSTEM
                    for ref, page in prepared.pages.items():
                        if page.document_id in full_ids:
                            allowed[ref] = [page.text]
                prompt = json.dumps(payload, ensure_ascii=False)
                body = json.dumps(DeepSeekAdapter().build_request(model=model, prompt=prompt, system_prompt=system),
                                  ensure_ascii=False, separators=(",", ":")).encode()
                # Never silently resend old full texts because a mixed context
                # is large. The reader sees the exact plan or a size error.
                if len(body) > MAX_FULL_TEXT_REQUEST_BYTES:
                    raise FullTextError("full_text_too_large")
                context = replace(prepared, prompt=prompt, request_bytes=len(body), allowed_quotes=allowed)
                return StudyPlan(prepared, context, key, scope, system, "analysis_reuse",
                                 tuple(memory.key for memory in selected), source_modes=modes, witnesses=witnesses)
        if prepared.request_bytes > MAX_FULL_TEXT_REQUEST_BYTES:
            raise FullTextError("full_text_too_large")
        return StudyPlan(prepared, prepared, key, scope, SYSTEM, "full_text", source_modes=full_modes)

    def store(self, plan: StudyPlan, question: str, model: str, raw: str) -> bool:
        output = parse_output(raw).model_dump_json()
        try:
            with self.database.transaction() as c:
                c.execute("""INSERT INTO full_text_study_cache(cache_key,scope_key,question,model,prompt_version,
                  document_order_json,output_json,context_kind,basis_keys_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(cache_key) DO UPDATE SET output_json=excluded.output_json,
                  document_order_json=excluded.document_order_json,context_kind=excluded.context_kind,
                  basis_keys_json=excluded.basis_keys_json,created_at=excluded.created_at""",
                  (plan.key, plan.scope_key, question.strip(), model, PROMPT_VERSION,
                   json.dumps(list(plan.prepared.versions)), output, plan.context_kind, json.dumps(plan.basis),
                   datetime.now(UTC).isoformat()))
                for doc, fingerprint in plan.prepared.fingerprints.items():
                    c.execute("""INSERT INTO full_text_study_documents(cache_key,document_id,text_fingerprint,read_mode)
                      VALUES(?,?,?,?) ON CONFLICT(cache_key,document_id) DO UPDATE SET
                      text_fingerprint=excluded.text_fingerprint,read_mode=excluded.read_mode""",
                      (plan.key, doc, fingerprint, plan.source_modes[doc]))
                # Upgrade verified legacy witnesses only after a successful,
                # current-source answer. No unverified backfill during migration.
                for witness_key, documents in plan.witnesses.items():
                    for doc, fingerprint in documents.items():
                        c.execute("""INSERT OR IGNORE INTO full_text_study_documents(
                          cache_key,document_id,text_fingerprint,read_mode) VALUES(?,?,?,'full_text')""",
                          (witness_key, doc, fingerprint))
        except sqlite3.Error:
            return False
        return True
