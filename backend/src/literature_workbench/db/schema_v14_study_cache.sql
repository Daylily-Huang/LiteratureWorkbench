CREATE TABLE full_text_study_cache (
  cache_key TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL,
  question TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  document_order_json TEXT NOT NULL,
  output_json TEXT NOT NULL,
  context_kind TEXT NOT NULL CHECK (context_kind IN ('full_text','analysis_reuse')),
  basis_keys_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX full_text_study_cache_scope ON full_text_study_cache(scope_key,context_kind,created_at DESC);
