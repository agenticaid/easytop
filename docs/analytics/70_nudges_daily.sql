-- The evening nudge: sent / no school / expired / failed per day, and the settings parents chose.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.nudges_daily` AS
SELECT day, env, synthetic, event, IFNULL(result, state) AS what, COUNT(*) AS n
FROM `${PROJECT}.${DATASET}.events`
WHERE event IN ('nudge', 'nudge_setting')
GROUP BY day, env, synthetic, event, what;
