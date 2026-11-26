ALTER TABLE document_relations ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual' CHECK(origin IN ('manual','ai_confirmed'));
ALTER TABLE document_relations ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE relation_analyses (
    id TEXT PRIMARY KEY, question TEXT NOT NULL, scope_json TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, fallback_reason TEXT,
    summary TEXT NOT NULL, warnings_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE relation_candidates (
    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES relation_analyses(id),
    proposal_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    revision INTEGER NOT NULL DEFAULT 0,
    relation_id TEXT UNIQUE REFERENCES document_relations(id) ON DELETE SET NULL,
    review_json TEXT, reviewed_at TEXT
);
CREATE INDEX relation_candidates_analysis ON relation_candidates(analysis_id);
