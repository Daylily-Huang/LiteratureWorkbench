CREATE TABLE rag_chunks(
  id TEXT PRIMARY KEY,
  content_sha256 TEXT NOT NULL,
  parse_run_id TEXT NOT NULL,
  physical_page INTEGER NOT NULL CHECK (physical_page >= 1),
  chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
  text TEXT NOT NULL,
  char_count INTEGER NOT NULL CHECK (char_count >= 0),
  created_at TEXT NOT NULL
);

CREATE INDEX rag_chunks_content ON rag_chunks(content_sha256,parse_run_id);

CREATE VIRTUAL TABLE rag_chunks_fts USING fts5(
  chunk_id UNINDEXED,
  physical_page UNINDEXED,
  text
);
