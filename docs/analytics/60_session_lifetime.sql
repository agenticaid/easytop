-- Panel 7: how long a Webtop login lives, by sign-in route. A login ends at the first `session_check` dead or
-- `session_expired` after it (before the parent's next login); a login still alive shows its latest healthy age as
-- a lower bound (state = 'alive').
CREATE OR REPLACE VIEW `${PROJECT}.${DATASET}.session_lifetime` AS
WITH logins AS (
  SELECT env, synthetic, parent_ref, route, ts AS login_ts,
    LEAD(ts) OVER (PARTITION BY env, synthetic, parent_ref ORDER BY ts) AS next_login_ts
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event = 'login_captured'
),
later AS (
  SELECT env, synthetic, parent_ref, ts, event, status, age_h
  FROM `${PROJECT}.${DATASET}.events`
  WHERE event IN ('session_check', 'session_expired')
)
SELECT
  l.env, l.synthetic, l.parent_ref, l.route, l.login_ts,
  IF(COUNTIF(e.status = 'dead' OR e.event = 'session_expired') > 0, 'ended', 'alive') AS state,
  ROUND(COALESCE(
    MIN(IF(e.status = 'dead', e.age_h, NULL)),
    MIN(IF(e.event = 'session_expired', TIMESTAMP_DIFF(e.ts, l.login_ts, SECOND) / 3600, NULL)),
    MAX(IF(e.status = 'ok', e.age_h, NULL)),
    0), 1) AS lifetime_hours
FROM logins l
LEFT JOIN later e
  ON e.env = l.env AND e.synthetic = l.synthetic AND e.parent_ref = l.parent_ref
  AND e.ts > l.login_ts AND (l.next_login_ts IS NULL OR e.ts < l.next_login_ts)
GROUP BY l.env, l.synthetic, l.parent_ref, l.route, l.login_ts;
