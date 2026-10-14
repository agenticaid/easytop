-- Friction: how often does a login run out? Per week (Sunday to Saturday, week = its Sunday): times the
-- "connection expired" button was shown (`connect_offered` with reason `expired`), active families (asked a
-- question, not rate-limited) and reconnects per active family. Complete days only: today is not in it.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.reconnects_weekly` AS
WITH active AS (
  SELECT env, synthetic, DATE_TRUNC(day, WEEK(SUNDAY)) AS week, COUNT(DISTINCT parent_ref) AS active_families
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome != 'rate_limited' AND day < CURRENT_DATE('Asia/Jerusalem')
  GROUP BY env, synthetic, week
),
expired AS (
  SELECT env, synthetic, DATE_TRUNC(day, WEEK(SUNDAY)) AS week, COUNT(*) AS reconnects
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'connect_offered' AND reason = 'expired' AND day < CURRENT_DATE('Asia/Jerusalem')
  GROUP BY env, synthetic, week
)
SELECT
  env, synthetic, week,
  IFNULL(a.active_families, 0) AS active_families,
  IFNULL(r.reconnects, 0) AS reconnects,
  SAFE_DIVIDE(IFNULL(r.reconnects, 0), a.active_families) AS reconnects_per_family
FROM active a
FULL OUTER JOIN expired r USING (env, synthetic, week);
