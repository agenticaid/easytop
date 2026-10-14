-- Panel 5: how many tool calls an answer needs, and which tools are used.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.tools_per_question` AS
SELECT day, env, synthetic, CAST(tool_count AS INT64) AS tool_calls, tool, COUNT(*) AS n
FROM `${PROJECT}.${DATASET}.events`
LEFT JOIN UNNEST(IF(ARRAY_LENGTH(tools) = 0, [CAST(NULL AS STRING)], tools)) AS tool
WHERE event = 'question' AND tool_count IS NOT NULL
GROUP BY day, env, synthetic, tool_calls, tool;
