-- Panels 1 and 5: answer time (model + Webtop + tools) per day, p50 / p95, and tool calls per answer.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.latency_daily` AS
SELECT
  day, env, synthetic,
  COUNT(*) AS answers,
  APPROX_QUANTILES(answer_ms, 100)[OFFSET(50)] / 1000 AS p50_seconds,
  APPROX_QUANTILES(answer_ms, 100)[OFFSET(95)] / 1000 AS p95_seconds,
  ROUND(AVG(tool_count), 2) AS avg_tool_calls
FROM `${PROJECT}.${DATASET}.events`
WHERE event = 'question' AND answer_ms IS NOT NULL
GROUP BY day, env, synthetic;
