-- North star: of the school evenings in the period, on what share did an active family ask about tomorrow?
-- Active family = asked at least one question (not rate-limited) in the window. Tomorrow question = category
-- `tomorrow`, any focus. School evening = Sunday to Thursday (Israel date), the question's own date, any hour.
-- coverage = (family, evening) pairs with a tomorrow question / (active families * school evenings).
-- One row per period from `periods` (7 / 28 / 90 days; periods_ago 0 = the latest, 1 = the one before it).
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.tomorrow_coverage` AS
WITH counted AS (
  SELECT
    p.env, p.synthetic, p.period_days, p.periods_ago, p.period_start, p.period_end, p.school_evenings,
    COUNT(DISTINCT q.parent_ref) AS active_families,
    COUNT(DISTINCT IF(q.category = 'tomorrow' AND EXTRACT(DAYOFWEEK FROM q.day) BETWEEN 1 AND 5,
      CONCAT(q.parent_ref, '|', CAST(q.day AS STRING)), NULL)) AS tomorrow_pairs
  FROM `${PROJECT}.${DATASET}.periods` p
  LEFT JOIN `${PROJECT}.${DATASET}.events` q
    ON q.event = 'question' AND q.outcome != 'rate_limited'
    AND q.env = p.env AND q.synthetic = p.synthetic
    AND q.day BETWEEN p.window_start AND p.period_end
  GROUP BY p.env, p.synthetic, p.period_days, p.periods_ago, p.period_start, p.period_end, p.school_evenings
)
SELECT
  env, synthetic, period_days, periods_ago, period_start, period_end,
  school_evenings, active_families, tomorrow_pairs,
  SAFE_DIVIDE(tomorrow_pairs, active_families * school_evenings) AS coverage
FROM counted;
