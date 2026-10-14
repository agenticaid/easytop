-- Panels 1 and 4: active parents, questions, cost per day and per question, share of input read from the cache.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.daily_usage` AS
SELECT
  day, env, synthetic,
  COUNT(DISTINCT parent_ref) AS active_parents,
  COUNTIF(outcome != 'rate_limited') AS questions,
  COUNTIF(follow_up) AS follow_ups,
  ROUND(SUM(IFNULL(cost_usd, 0)), 4) AS cost_usd,
  ROUND(SAFE_DIVIDE(SUM(cost_usd), COUNTIF(cost_usd IS NOT NULL)), 4) AS cost_per_question_usd,
  ROUND(SAFE_DIVIDE(SUM(cache_read_tokens),
    SUM(IFNULL(input_tokens, 0) + IFNULL(cache_read_tokens, 0) + IFNULL(cache_write_tokens, 0))), 3) AS cache_share,
  SUM(IFNULL(input_tokens, 0)) AS input_tokens,
  SUM(IFNULL(output_tokens, 0)) AS output_tokens
FROM `${PROJECT}.${DATASET}.events`
WHERE event = 'question'
GROUP BY day, env, synthetic;
