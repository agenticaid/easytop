-- Panel 7 for the dashboard: how long a Webtop login lives, by sign-in route, as counts and medians only (the
-- per-login rows of `session_lifetime` carry a pseudonym, so the app never reads that view). state `ended` = the
-- login died; `alive` = still valid, so the hours are a lower bound.
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.session_lifetime_by_route` AS
SELECT DISTINCT
  env, synthetic, route, state,
  COUNT(*) OVER w AS logins,
  ROUND(PERCENTILE_CONT(lifetime_hours, 0.5) OVER w, 1) AS median_hours,
  MAX(lifetime_hours) OVER w AS max_hours
FROM `${PROJECT}.${DATASET}.session_lifetime`
WINDOW w AS (PARTITION BY env, synthetic, route, state);
