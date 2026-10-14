-- Is it fast, reliable and cheap enough? Per period from `periods` (7 / 28 / 90 days; periods_ago 0 = the latest,
-- 1 = the one before it): questions (not rate-limited), active families, cost, cost per active family scaled to
-- 30 days, p95 answer time (nearest rank: exact, not approximated), failure share (error, truncated, tool_limit,
-- session_expired) and no-data share (answered_no_data).
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.worth_it` AS
WITH joined AS (
  SELECT
    p.env, p.synthetic, p.period_days, p.periods_ago, p.period_start, p.period_end, p.window_days,
    COUNT(q.ts) AS questions,
    COUNT(DISTINCT q.parent_ref) AS active_families,
    SUM(IFNULL(q.cost_usd, 0)) AS cost_usd,
    COUNTIF(q.outcome IN ('error', 'truncated', 'tool_limit', 'session_expired')) AS failures,
    COUNTIF(q.outcome = 'answered_no_data') AS no_data,
    ARRAY_AGG(q.answer_ms IGNORE NULLS ORDER BY q.answer_ms) AS answer_ms_sorted
  FROM `${PROJECT}.${DATASET}.periods` p
  LEFT JOIN `${PROJECT}.${DATASET}.events` q
    ON q.event = 'question' AND q.outcome != 'rate_limited'
    AND q.env = p.env AND q.synthetic = p.synthetic
    AND q.day BETWEEN p.window_start AND p.period_end
  GROUP BY p.env, p.synthetic, p.period_days, p.periods_ago, p.period_start, p.period_end, p.window_days
)
SELECT
  env, synthetic, period_days, periods_ago, period_start, period_end,
  questions, active_families,
  ROUND(cost_usd, 4) AS cost_usd,
  ROUND(SAFE_DIVIDE(cost_usd, active_families) * 30 / NULLIF(window_days, 0), 4) AS cost_per_family_month_usd,
  answer_ms_sorted[SAFE_OFFSET(DIV(95 * ARRAY_LENGTH(answer_ms_sorted) + 99, 100) - 1)] / 1000 AS p95_seconds,
  failures, SAFE_DIVIDE(failures, questions) AS failure_share,
  no_data, SAFE_DIVIDE(no_data, questions) AS no_data_share
FROM joined;
