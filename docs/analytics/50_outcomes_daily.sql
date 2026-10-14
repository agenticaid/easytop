-- Panel 6: how each question ended. out_of_scope / read_only / injection cannot be told apart by code: they are
-- inside answered_no_data, and the guardrail evals prove them (Spec 07).
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.outcomes_daily` AS
SELECT day, env, synthetic, outcome, COUNT(*) AS questions
FROM `${PROJECT}.${DATASET}.events`
WHERE event = 'question'
GROUP BY day, env, synthetic, outcome;
