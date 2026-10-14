-- Activation speed: per family, minutes from the first time the connect button was shown to the first question
-- with outcome `answered`. Only the median and how many families it covers leave this view. A family whose first
-- answer came before its first connect button (it was already connected) is left out.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.time_to_first_answer` AS
WITH offered AS (
  SELECT env, synthetic, parent_ref, MIN(ts) AS offered_ts
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'connect_offered'
  GROUP BY env, synthetic, parent_ref
),
answered AS (
  SELECT env, synthetic, parent_ref, MIN(ts) AS answered_ts
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome = 'answered'
  GROUP BY env, synthetic, parent_ref
),
waits AS (
  SELECT o.env, o.synthetic, TIMESTAMP_DIFF(a.answered_ts, o.offered_ts, MILLISECOND) / 60000 AS minutes
  FROM offered o
  JOIN answered a ON a.env = o.env AND a.synthetic = o.synthetic AND a.parent_ref = o.parent_ref
  WHERE a.answered_ts >= o.offered_ts
)
SELECT DISTINCT
  env, synthetic,
  COUNT(*) OVER w AS families,
  ROUND(PERCENTILE_CONT(minutes, 0.5) OVER w, 1) AS median_minutes
FROM waits
WINDOW w AS (PARTITION BY env, synthetic);
