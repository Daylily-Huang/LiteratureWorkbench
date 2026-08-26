ALTER TABLE rag_chunks ADD COLUMN rects_json TEXT;

DROP TABLE IF EXISTS rag_chunks_fts;

CREATE VIRTUAL TABLE rag_chunks_fts USING fts5(
  chunk_id UNINDEXED,
  physical_page UNINDEXED,
  text,
  tokenize='trigram'
);

-- Force lazy re-chunking on next ask: block-aware chunks carry bbox rects
-- and the trigram index must be rebuilt from fresh chunk text anyway.
DELETE FROM rag_chunks;
