CREATE TABLE IF NOT EXISTS deepseek_usage (
  audit_id TEXT PRIMARY KEY REFERENCES egress_audit(id),
  recorded_at TEXT NOT NULL,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_cache_hit_tokens INTEGER NOT NULL CHECK (prompt_cache_hit_tokens >= 0),
  prompt_cache_miss_tokens INTEGER NOT NULL CHECK (prompt_cache_miss_tokens >= 0),
  completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
  total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
  cost_nano_cny INTEGER CHECK (cost_nano_cny IS NULL OR cost_nano_cny >= 0),
  pricing_version TEXT,
  cache_hit_nano_cny_per_token INTEGER,
  cache_miss_nano_cny_per_token INTEGER,
  output_nano_cny_per_token INTEGER,
  CHECK (
    (cost_nano_cny IS NULL AND pricing_version IS NULL
      AND cache_hit_nano_cny_per_token IS NULL
      AND cache_miss_nano_cny_per_token IS NULL
      AND output_nano_cny_per_token IS NULL)
    OR
    (cost_nano_cny IS NOT NULL AND pricing_version IS NOT NULL
      AND cache_hit_nano_cny_per_token IS NOT NULL
      AND cache_miss_nano_cny_per_token IS NOT NULL
      AND output_nano_cny_per_token IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS deepseek_usage_recorded_at
  ON deepseek_usage(recorded_at);
