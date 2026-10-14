-- One typed row per analytics event. Reads the JSON through JSON_VALUE, so it works before the log sink has
-- created a column for every field. `events_raw` (ts, j) is written by tools/ops/analytics_views.sh: the log sink
-- table plus, in dev, the synthetic table. Every view below has `env` and `synthetic`: filter on them.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.events` AS
SELECT
  TIMESTAMP(JSON_VALUE(j, '$.ts')) AS ts,
  DATE(TIMESTAMP(JSON_VALUE(j, '$.ts')), 'Asia/Jerusalem') AS day,
  JSON_VALUE(j, '$.event') AS event,
  JSON_VALUE(j, '$.env') AS env,
  COALESCE(SAFE_CAST(JSON_VALUE(j, '$.synthetic') AS BOOL), FALSE) AS synthetic,
  JSON_VALUE(j, '$.parent_ref') AS parent_ref,
  -- question
  JSON_VALUE(j, '$.outcome') AS outcome,
  JSON_VALUE(j, '$.category') AS category,
  JSON_VALUE(j, '$.focus') AS focus,
  JSON_VALUE(j, '$.kids_scope') AS kids_scope,
  JSON_VALUE(j, '$.language') AS language,
  SAFE_CAST(JSON_VALUE(j, '$.follow_up') AS BOOL) AS follow_up,
  JSON_VALUE(j, '$.model') AS model,
  SAFE_CAST(JSON_VALUE(j, '$.answer_ms') AS FLOAT64) AS answer_ms,
  SAFE_CAST(JSON_VALUE(j, '$.tool_count') AS FLOAT64) AS tool_count,
  JSON_VALUE_ARRAY(j, '$.tools') AS tools,
  SAFE_CAST(JSON_VALUE(j, '$.input_tokens') AS FLOAT64) AS input_tokens,
  SAFE_CAST(JSON_VALUE(j, '$.output_tokens') AS FLOAT64) AS output_tokens,
  SAFE_CAST(JSON_VALUE(j, '$.cache_read_tokens') AS FLOAT64) AS cache_read_tokens,
  SAFE_CAST(JSON_VALUE(j, '$.cache_write_tokens') AS FLOAT64) AS cache_write_tokens,
  SAFE_CAST(JSON_VALUE(j, '$.cost_usd') AS FLOAT64) AS cost_usd,
  JSON_VALUE(j, '$.error_type') AS error_type,
  -- funnel, login, onboarding
  JSON_VALUE(j, '$.reason') AS reason,
  JSON_VALUE(j, '$.route') AS route,
  SAFE_CAST(JSON_VALUE(j, '$.login_seconds') AS FLOAT64) AS login_seconds,
  SAFE_CAST(JSON_VALUE(j, '$.cookie_hours') AS FLOAT64) AS cookie_hours,
  SAFE_CAST(JSON_VALUE(j, '$.taps') AS FLOAT64) AS taps,
  SAFE_CAST(JSON_VALUE(j, '$.frames_dropped') AS FLOAT64) AS frames_dropped,
  SAFE_CAST(JSON_VALUE(j, '$.kid_count') AS FLOAT64) AS kid_count,
  SAFE_CAST(JSON_VALUE(j, '$.school_count') AS FLOAT64) AS school_count,
  SAFE_CAST(JSON_VALUE(j, '$.reconnect') AS BOOL) AS reconnect,
  SAFE_CAST(JSON_VALUE(j, '$.ttl_hours') AS FLOAT64) AS ttl_hours,
  -- nudge, health, errors
  JSON_VALUE(j, '$.state') AS state,
  SAFE_CAST(JSON_VALUE(j, '$.hour') AS FLOAT64) AS hour,
  JSON_VALUE(j, '$.result') AS result,
  JSON_VALUE(j, '$.status') AS status,
  SAFE_CAST(JSON_VALUE(j, '$.age_h') AS FLOAT64) AS age_h,
  JSON_VALUE(j, '$.where') AS error_where
FROM `${PROJECT}.${DATASET}.events_raw`
WHERE JSON_VALUE(j, '$.event') IS NOT NULL;
