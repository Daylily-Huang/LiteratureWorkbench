CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS libraries (
  id TEXT PRIMARY KEY,
  root_path TEXT NOT NULL,
  root_path_key TEXT NOT NULL UNIQUE,
  root_volume_id TEXT NOT NULL,
  root_file_id TEXT NOT NULL,
  recursive INTEGER NOT NULL CHECK (recursive IN (0,1)),
  discovery_mode TEXT NOT NULL CHECK (discovery_mode IN ('monitored','managed_import')),
  cloud_pdf_auto_allowed INTEGER NOT NULL CHECK (cloud_pdf_auto_allowed IN (0,1)),
  authorization_source TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contents (
  sha256 TEXT PRIMARY KEY,
  size_bytes INTEGER NOT NULL,
  mime_type TEXT NOT NULL,
  page_count INTEGER,
  current_parse_run_id TEXT REFERENCES parse_runs(id),
  title TEXT,
  authors_json TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id),
  content_sha256 TEXT REFERENCES contents(sha256),
  path TEXT NOT NULL,
  path_key TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  format TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  volume_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  hard_link_count INTEGER NOT NULL,
  parse_preference TEXT NOT NULL CHECK (parse_preference IN ('parse','read_only')),
  parse_strategy_override TEXT CHECK (parse_strategy_override IN ('auto','pymupdf','mineru')),
  ocr_language_override TEXT CHECK (ocr_language_override IN ('en','ch')),
  auto_cloud_eligible INTEGER NOT NULL CHECK (auto_cloud_eligible IN (0,1)),
  authorization_source TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  content_sha256 TEXT REFERENCES contents(sha256),
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  volume_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  hard_link_count INTEGER NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parse_runs (
  id TEXT PRIMARY KEY,
  content_sha256 TEXT NOT NULL REFERENCES contents(sha256),
  parser_name TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  status TEXT NOT NULL,
  output_dir TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pages (
  content_sha256 TEXT NOT NULL REFERENCES contents(sha256),
  page_index INTEGER NOT NULL CHECK (page_index >= 0),
  physical_page INTEGER NOT NULL CHECK (physical_page = page_index + 1),
  parse_run_id TEXT NOT NULL REFERENCES parse_runs(id),
  normalized_text TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  width_points REAL NOT NULL CHECK (width_points > 0),
  height_points REAL NOT NULL CHECK (height_points > 0),
  blocks_json TEXT NOT NULL,
  coordinate_reliable INTEGER NOT NULL CHECK (coordinate_reliable IN (0,1)),
  PRIMARY KEY (parse_run_id, page_index)
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  dedup_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT,
  error_code TEXT,
  retryable INTEGER NOT NULL DEFAULT 1 CHECK (retryable IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedup
  ON jobs(dedup_key) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS translation_cache (
  cache_key TEXT PRIMARY KEY,
  translated_text TEXT NOT NULL,
  detected_source_language TEXT NOT NULL,
  target_language TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_validations (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  role TEXT NOT NULL,
  model TEXT NOT NULL,
  validated_at TEXT NOT NULL,
  UNIQUE(provider, role, model)
);
CREATE TABLE IF NOT EXISTS model_validation_tasks (
  id TEXT PRIMARY KEY,
  session_generation TEXT NOT NULL,
  client_task_id TEXT NOT NULL,
  base_settings_revision INTEGER NOT NULL,
  proposal_revision INTEGER NOT NULL UNIQUE,
  proposed_fast_model TEXT NOT NULL,
  proposed_strong_model TEXT NOT NULL,
  fast_probe_task_id TEXT NOT NULL UNIQUE,
  strong_probe_task_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  error_code TEXT,
  resulting_settings_revision INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(session_generation, client_task_id)
);
CREATE TABLE IF NOT EXISTS translation_tasks (
  id TEXT PRIMARY KEY,
  session_generation TEXT NOT NULL,
  client_task_id TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(id),
  document_version_id TEXT NOT NULL REFERENCES document_versions(id),
  content_sha256 TEXT NOT NULL REFERENCES contents(sha256),
  physical_page INTEGER NOT NULL CHECK (physical_page >= 1),
  selected_text TEXT NOT NULL,
  context_before TEXT NOT NULL,
  context_after TEXT NOT NULL,
  target_language TEXT NOT NULL,
  route_mode TEXT NOT NULL,
  requested_provider TEXT,
  requested_model TEXT,
  model_validation_id TEXT REFERENCES model_validations(id),
  settings_revision INTEGER NOT NULL,
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  fallback_reason TEXT,
  result_cache_key TEXT REFERENCES translation_cache(cache_key),
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(session_generation, client_task_id)
);
CREATE TABLE IF NOT EXISTS import_intents (
  id TEXT PRIMARY KEY,
  session_generation TEXT NOT NULL,
  file_name TEXT NOT NULL,
  expected_size INTEGER NOT NULL,
  expected_media_type TEXT NOT NULL,
  upload_token_hash TEXT,
  status TEXT NOT NULL,
  content_sha256 TEXT,
  final_path_key TEXT,
  error_code TEXT,
  consumed_at TEXT,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (status = 'pending' AND upload_token_hash IS NOT NULL) OR
    (status <> 'pending' AND upload_token_hash IS NULL)
  )
);
CREATE TABLE IF NOT EXISTS confirmed_imports (
  intent_id TEXT PRIMARY KEY REFERENCES import_intents(id),
  document_id TEXT NOT NULL REFERENCES documents(id),
  content_sha256 TEXT NOT NULL REFERENCES contents(sha256),
  final_path_key TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_promotions (
  content_sha256 TEXT PRIMARY KEY,
  owner_intent_id TEXT NOT NULL UNIQUE REFERENCES import_intents(id),
  final_path_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('claimed','moved','completed','failed')),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS egress_audit (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  process_generation TEXT NOT NULL,
  event_time TEXT NOT NULL,
  authorization_type TEXT NOT NULL,
  document_id TEXT,
  content_sha256 TEXT,
  display_file_name TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  payload_kind TEXT NOT NULL,
  payload_bytes INTEGER NOT NULL,
  payload_sha256 TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail_code TEXT,
  latency_ms INTEGER,
  UNIQUE(task_id, purpose, attempt_no)
);
CREATE INDEX IF NOT EXISTS egress_audit_task ON egress_audit(task_id, purpose, attempt_no);
CREATE TABLE IF NOT EXISTS one_time_egress_grants (
  id TEXT PRIMARY KEY,
  file_identity_digest TEXT NOT NULL,
  task_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  payload_kind TEXT NOT NULL,
  max_bytes INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_at TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_library_status ON documents(library_id, status);
CREATE INDEX IF NOT EXISTS documents_content ON documents(content_sha256);
CREATE INDEX IF NOT EXISTS documents_file_identity ON documents(library_id, volume_id, file_id);
CREATE INDEX IF NOT EXISTS document_versions_document ON document_versions(document_id, observed_at);
CREATE INDEX IF NOT EXISTS confirmed_imports_document ON confirmed_imports(document_id, confirmed_at);
CREATE INDEX IF NOT EXISTS confirmed_imports_final_path ON confirmed_imports(final_path_key);
CREATE INDEX IF NOT EXISTS import_promotions_owner ON import_promotions(owner_intent_id);
CREATE INDEX IF NOT EXISTS pages_content_page ON pages(content_sha256, physical_page, parse_run_id);
