ALTER TABLE deepseek_usage RENAME TO deepseek_usage_v05;

CREATE TABLE deepseek_usage (
  audit_id TEXT PRIMARY KEY REFERENCES egress_audit(id),
  recorded_at TEXT NOT NULL,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_cache_hit_tokens INTEGER,
  prompt_cache_miss_tokens INTEGER,
  completion_tokens INTEGER,
  total_tokens INTEGER,
  cost_nano_cny INTEGER CHECK (cost_nano_cny IS NULL OR cost_nano_cny >= 0),
  pricing_version TEXT,
  cache_hit_nano_cny_per_token INTEGER,
  cache_miss_nano_cny_per_token INTEGER,
  output_nano_cny_per_token INTEGER,
  CHECK (
    (prompt_cache_hit_tokens IS NULL
      AND prompt_cache_miss_tokens IS NULL
      AND completion_tokens IS NULL
      AND total_tokens IS NULL)
    OR
    (prompt_cache_hit_tokens IS NOT NULL
      AND prompt_cache_miss_tokens IS NOT NULL
      AND completion_tokens IS NOT NULL
      AND total_tokens IS NOT NULL
      AND prompt_cache_hit_tokens >= 0
      AND prompt_cache_miss_tokens >= 0
      AND completion_tokens >= 0
      AND total_tokens >= 0)
  ),
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

INSERT INTO deepseek_usage(
  audit_id,recorded_at,purpose,model,prompt_cache_hit_tokens,
  prompt_cache_miss_tokens,completion_tokens,total_tokens,cost_nano_cny,
  pricing_version,cache_hit_nano_cny_per_token,cache_miss_nano_cny_per_token,
  output_nano_cny_per_token)
SELECT
  audit_id,recorded_at,purpose,model,prompt_cache_hit_tokens,
  prompt_cache_miss_tokens,completion_tokens,total_tokens,cost_nano_cny,
  pricing_version,cache_hit_nano_cny_per_token,cache_miss_nano_cny_per_token,
  output_nano_cny_per_token
FROM deepseek_usage_v05;

DROP TABLE deepseek_usage_v05;

CREATE INDEX deepseek_usage_recorded_at ON deepseek_usage(recorded_at);
