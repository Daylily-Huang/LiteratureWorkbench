CREATE TABLE full_text_study_documents (
  cache_key TEXT NOT NULL REFERENCES full_text_study_cache(cache_key) ON DELETE CASCADE,
  document_id TEXT NOT NULL,
  text_fingerprint TEXT NOT NULL,
  read_mode TEXT NOT NULL CHECK (read_mode IN ('full_text','analysis_reuse')),
  PRIMARY KEY (cache_key,document_id)
);
CREATE INDEX full_text_study_documents_lookup ON full_text_study_documents(document_id,text_fingerprint,read_mode);
