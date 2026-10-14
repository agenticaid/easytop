-- Do they come back? Per week (Sunday to Saturday, week = its Sunday): families that asked at least one question
-- (not rate-limited), and how many of them were active on 1, 2, 3, 4 or 5+ different days that week. Only counts
-- leave this view, never a family. Complete days only: today is not in it.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.habit_weekly` AS
WITH family_weeks AS (
  SELECT env, synthetic, parent_ref, DATE_TRUNC(day, WEEK(SUNDAY)) AS week, COUNT(DISTINCT day) AS days
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome != 'rate_limited'
    AND day < CURRENT_DATE('Asia/Jerusalem')
  GROUP BY env, synthetic, parent_ref, week
)
SELECT
  env, synthetic, week,
  COUNT(*) AS active_families,
  COUNTIF(days = 1) AS days_1,
  COUNTIF(days = 2) AS days_2,
  COUNTIF(days = 3) AS days_3,
  COUNTIF(days = 4) AS days_4,
  COUNTIF(days >= 5) AS days_5plus
FROM family_weeks
GROUP BY env, synthetic, week;
