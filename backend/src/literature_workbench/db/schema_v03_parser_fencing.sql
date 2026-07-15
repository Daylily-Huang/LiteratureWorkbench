ALTER TABLE contents ADD COLUMN parse_request_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE parse_runs ADD COLUMN request_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE parse_runs ADD COLUMN source_job_id TEXT REFERENCES jobs(id);
CREATE INDEX IF NOT EXISTS parse_runs_source_job ON parse_runs(source_job_id);
