CREATE TABLE IF NOT EXISTS pdf_annotation_baselines (
  document_id TEXT PRIMARY KEY REFERENCES documents(id),
  content_sha256 TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdf_annotation_mutations (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  annotation_id TEXT,
  operation TEXT NOT NULL CHECK (operation IN ('create','update','delete','restore')),
  pre_content_sha256 TEXT NOT NULL,
  post_content_sha256 TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  undone_at TEXT
);

CREATE INDEX IF NOT EXISTS pdf_annotation_mutations_document
  ON pdf_annotation_mutations(document_id, created_at, id);
