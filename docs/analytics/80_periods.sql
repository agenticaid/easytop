-- Helper for the dashboard (no families in it): the last 7, 28 and 90 COMPLETE days (Israel dates, yesterday is the
-- last one, so an evening that has not happened yet never counts against anyone) and the same number of days right
-- before them, for the "compared with the previous period" sentences. Per env and synthetic flag. A window starts
-- no earlier than the first question ever asked there (`window_start`), so a young product is not judged on days
-- before it existed. `school_evenings` = Sunday to Thursday dates in the window (v1: holidays count as school days).
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.periods` AS
WITH first_day AS (
  SELECT env, synthetic, MIN(day) AS first_day
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome != 'rate_limited'
  GROUP BY env, synthetic
),
grid AS (
  SELECT f.env, f.synthetic, f.first_day, n AS period_days, ago AS periods_ago,
    DATE_SUB(CURRENT_DATE('Asia/Jerusalem'), INTERVAL 1 + ago * n DAY) AS period_end
  FROM first_day f, UNNEST([7, 28, 90]) AS n, UNNEST([0, 1]) AS ago
),
clipped AS (
  SELECT env, synthetic, period_days, periods_ago, period_end,
    DATE_SUB(period_end, INTERVAL period_days - 1 DAY) AS period_start,
    GREATEST(DATE_SUB(period_end, INTERVAL period_days - 1 DAY), first_day) AS window_start
  FROM grid
)
SELECT
  env, synthetic, period_days, periods_ago, period_start, period_end, window_start,
  GREATEST(DATE_DIFF(period_end, window_start, DAY) + 1, 0) AS window_days,
  (SELECT COUNTIF(EXTRACT(DAYOFWEEK FROM d) BETWEEN 1 AND 5)
   FROM UNNEST(GENERATE_DATE_ARRAY(window_start, period_end)) AS d) AS school_evenings
FROM clipped;
