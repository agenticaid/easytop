#!/usr/bin/env bash
# Create or replace the BigQuery views behind the analytics dashboard (SQL in docs/analytics/). Safe to run again.
#   tools/ops/analytics_views.sh PROJECT_ID dev|prod
# `events_raw` unions whichever source tables exist: the log sink's table (after the first real event) and, in dev, the
# synthetic table. Run it again after the sink table first appears.
set -euo pipefail

PROJECT="${1:?usage: analytics_views.sh PROJECT_ID dev|prod}"
ENVIRONMENT="${2:?usage: analytics_views.sh PROJECT_ID dev|prod}"
case "$ENVIRONMENT" in dev|prod) ;; *) echo "environment must be dev or prod" >&2; exit 2 ;; esac
DATASET="easytop_analytics_${ENVIRONMENT}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
BQ=(bq --project_id "$PROJECT" query --use_legacy_sql=false --quiet)
# SQL goes in on stdin: as an argument, a leading "-- comment" would be read as a command-line flag.
run_sql() { printf '%s\n' "$1" | "${BQ[@]}"; }

has_table() { bq --project_id "$PROJECT" show "${PROJECT}:${DATASET}.$1" >/dev/null 2>&1; }

PARTS=()
# The sink names its table after the log: Python logging writes to stderr, so it is run_googleapis_com_stderr; a
# stdout table is read too if one ever appears.
for stream in stderr stdout; do
  if has_table "run_googleapis_com_${stream}"; then
    PARTS+=("SELECT TO_JSON_STRING(jsonPayload) AS j FROM \`${PROJECT}.${DATASET}.run_googleapis_com_${stream}\`")
  fi
done
if has_table synthetic_events; then
  PARTS+=("SELECT payload AS j FROM \`${PROJECT}.${DATASET}.synthetic_events\`")
fi
if [ ${#PARTS[@]} -eq 0 ]; then
  echo "no source table yet in ${DATASET}: run analytics_setup.sh, wait for the first event (or load synthetic data), then run this again" >&2
  exit 1
fi
UNION="${PARTS[0]}"
for part in "${PARTS[@]:1}"; do UNION+=$'\nUNION ALL\n'"$part"; done
run_sql "CREATE OR REPLACE VIEW \`${PROJECT}.${DATASET}.events_raw\` AS ${UNION}"
echo "view events_raw over ${#PARTS[@]} source table(s)"

for file in "$HERE"/docs/analytics/[0-9]*.sql; do
  sql="$(sed -e "s/\${PROJECT}/${PROJECT}/g" -e "s/\${DATASET}/${DATASET}/g" "$file")"
  run_sql "$sql"
  echo "view from $(basename "$file")"
done
