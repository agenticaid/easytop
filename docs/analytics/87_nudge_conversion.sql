-- Does the evening nudge get used? Per day: nudges `sent`, and how many were followed by a question (not
-- rate-limited) from the same family within 30 minutes. Counts only.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.nudge_conversion` AS
WITH sent AS (
  SELECT env, synthetic, parent_ref, ts, day
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'nudge' AND result = 'sent'
),
asked AS (
  SELECT env, synthetic, parent_ref, ts
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome != 'rate_limited'
),
flagged AS (
  SELECT s.env, s.synthetic, s.day, s.parent_ref, s.ts, LOGICAL_OR(a.ts IS NOT NULL) AS followed
  FROM sent s
  LEFT JOIN asked a
    ON a.env = s.env AND a.synthetic = s.synthetic AND a.parent_ref = s.parent_ref
    AND a.ts > s.ts AND a.ts <= TIMESTAMP_ADD(s.ts, INTERVAL 30 MINUTE)
  GROUP BY s.env, s.synthetic, s.day, s.parent_ref, s.ts
)
SELECT env, synthetic, day, COUNT(*) AS sent, COUNTIF(followed) AS followed
FROM flagged
GROUP BY env, synthetic, day;
