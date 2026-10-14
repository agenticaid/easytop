-- Panel 6: failures by place and exception type (never the message), plus model errors inside answers.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.errors_daily` AS
SELECT day, env, synthetic, error_where AS place, error_type, COUNT(*) AS n
FROM `${PROJECT}.${DATASET}.events`
WHERE event = 'error'
GROUP BY day, env, synthetic, place, error_type
UNION ALL
SELECT day, env, synthetic, 'answer' AS place, error_type, COUNT(*) AS n
FROM `${PROJECT}.${DATASET}.events`
WHERE event = 'question' AND outcome = 'error'
GROUP BY day, env, synthetic, place, error_type;
