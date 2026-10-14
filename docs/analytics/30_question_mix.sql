-- Panel 3: what parents ask, from labels derived by code (no question text exists anywhere).
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.question_mix` AS
SELECT
  day, env, synthetic, category, focus, kids_scope, language, follow_up,
  COUNT(*) AS questions
FROM `${PROJECT}.${DATASET}.events`
WHERE event = 'question' AND outcome != 'rate_limited'
GROUP BY day, env, synthetic, category, focus, kids_scope, language, follow_up;
