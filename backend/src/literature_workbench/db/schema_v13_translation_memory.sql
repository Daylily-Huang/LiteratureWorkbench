ALTER TABLE translation_tasks ADD COLUMN page_text_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE translation_tasks ADD COLUMN prefer_exact INTEGER NOT NULL DEFAULT 0;
ALTER TABLE translation_tasks ADD COLUMN reuse_details TEXT NOT NULL DEFAULT '{}';

CREATE TABLE translation_memory (
  memory_key TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  physical_page INTEGER NOT NULL,
  page_text_hash TEXT NOT NULL,
  source_text TEXT NOT NULL,
  translated_text TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  target_language TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  source_task_id TEXT NOT NULL REFERENCES translation_tasks(id),
  created_at TEXT NOT NULL
);
CREATE INDEX translation_memory_lookup ON translation_memory
  (document_id,physical_page,page_text_hash,provider,model,target_language,prompt_version);
CREATE INDEX translation_history_lookup ON translation_tasks
  (document_id,physical_page,provider,model,status);
