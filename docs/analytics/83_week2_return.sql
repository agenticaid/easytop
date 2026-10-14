-- Week-2 return: of the families whose first answered question is at least 14 days old, how many asked again
-- 7 to 13 days after that first answered question? One row per env and synthetic flag, counts only.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.week2_return` AS
WITH firsts AS (
  SELECT env, synthetic, parent_ref, MIN(day) AS first_day
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome = 'answered'
  GROUP BY env, synthetic, parent_ref
),
asked AS (
  SELECT DISTINCT env, synthetic, parent_ref, day
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome != 'rate_limited'
)
SELECT
  f.env, f.synthetic,
  COUNT(DISTINCT f.parent_ref) AS families,
  COUNT(DISTINCT IF(a.day IS NOT NULL, f.parent_ref, NULL)) AS returned
FROM firsts f
LEFT JOIN asked a
  ON a.env = f.env AND a.synthetic = f.synthetic AND a.parent_ref = f.parent_ref
  AND a.day BETWEEN DATE_ADD(f.first_day, INTERVAL 7 DAY) AND DATE_ADD(f.first_day, INTERVAL 13 DAY)
WHERE f.first_day <= DATE_SUB(CURRENT_DATE('Asia/Jerusalem'), INTERVAL 14 DAY)
GROUP BY f.env, f.synthetic;
