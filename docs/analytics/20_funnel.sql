-- Panel 2: how many parents reached each onboarding step (ever), and who came back on a second day.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.funnel` AS
WITH q AS (
  SELECT env, synthetic, parent_ref, COUNT(DISTINCT day) AS days
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'question' AND outcome IN ('answered', 'answered_no_data')
  GROUP BY env, synthetic, parent_ref
),
steps AS (
  SELECT env, synthetic, 1 AS step_order, '1 invite created' AS step, COUNT(*) AS n
    FROM `${PROJECT}.${DATASET}.events` WHERE event = 'invite_created' GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 2, '2 invite redeemed', COUNT(DISTINCT parent_ref)
    FROM `${PROJECT}.${DATASET}.events` WHERE event = 'invite_redeemed' GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 3, '3 connect button shown', COUNT(DISTINCT parent_ref)
    FROM `${PROJECT}.${DATASET}.events` WHERE event = 'connect_offered' GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 4, '4 login page opened', COUNT(DISTINCT parent_ref)
    FROM `${PROJECT}.${DATASET}.events` WHERE event = 'connect_opened' GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 5, '5 signed in to Webtop', COUNT(DISTINCT parent_ref)
    FROM `${PROJECT}.${DATASET}.events` WHERE event = 'login_captured' GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 6, '6 kids confirmed', COUNT(DISTINCT parent_ref)
    FROM `${PROJECT}.${DATASET}.events` WHERE event = 'kids_confirmed' GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 7, '7 first question', COUNT(*) FROM q GROUP BY env, synthetic
  UNION ALL SELECT env, synthetic, 8, '8 asked on 2+ days', COUNTIF(days >= 2) FROM q GROUP BY env, synthetic
)
SELECT * FROM steps;
