#!/usr/bin/env bash
# Content-free analytics (task 6.11): a BigQuery dataset per environment and a Cloud Logging sink that copies ONLY the
# analytics events (log lines with an `event` key) of that environment's Cloud Run service into it. The ops lines with
# Telegram ids never reach BigQuery. Safe to run again: every step checks first.
#   tools/ops/analytics_setup.sh PROJECT_ID LOCATION dev|prod
# Example: tools/ops/analytics_setup.sh your-gcp-project me-west1 dev
# Then: tools/ops/analytics_views.sh PROJECT_ID dev   (after the first event has arrived)
set -euo pipefail

PROJECT="${1:?usage: analytics_setup.sh PROJECT_ID LOCATION dev|prod}"
LOCATION="${2:?usage: analytics_setup.sh PROJECT_ID LOCATION dev|prod}"
ENVIRONMENT="${3:?usage: analytics_setup.sh PROJECT_ID LOCATION dev|prod}"
case "$ENVIRONMENT" in dev|prod) ;; *) echo "environment must be dev or prod" >&2; exit 2 ;; esac
DATASET="easytop_analytics_${ENVIRONMENT}"
SINK="easytop-analytics-${ENVIRONMENT}"
SERVICE="easytop-${ENVIRONMENT}"
FILTER="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND jsonPayload.event:* AND jsonPayload.schema:*"
GC=(gcloud --project "$PROJECT")

# 1. The dataset (events are kept 400 days; a partition older than that is dropped by BigQuery).
if bq --project_id "$PROJECT" show --dataset "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
  echo "dataset ${DATASET} exists"
else
  bq --project_id "$PROJECT" mk --dataset --location "$LOCATION" \
    --default_partition_expiration $((400 * 24 * 3600)) \
    --description "Easytop ${ENVIRONMENT}: content-free analytics events (no text, no names, pseudonymous parents)" \
    "${PROJECT}:${DATASET}"
fi

# 2. Dev only: a table for the synthetic demo events (tools/analytics/synthetic.py), one JSON string per row.
if [ "$ENVIRONMENT" = dev ]; then
  if bq --project_id "$PROJECT" show "${PROJECT}:${DATASET}.synthetic_events" >/dev/null 2>&1; then
    echo "table synthetic_events exists"
  else
    bq --project_id "$PROJECT" mk --table \
      --description "SYNTHETIC demo events (FakeConnector family, never real data); every row has synthetic=true" \
      "${PROJECT}:${DATASET}.synthetic_events" payload:STRING
  fi
fi

# 3. The log sink: only analytics events of this environment's service, into date-partitioned tables.
DESTINATION="bigquery.googleapis.com/projects/${PROJECT}/datasets/${DATASET}"
if "${GC[@]}" logging sinks describe "$SINK" >/dev/null 2>&1; then
  "${GC[@]}" logging sinks update "$SINK" "$DESTINATION" --log-filter="$FILTER" --use-partitioned-tables
  echo "sink ${SINK} updated"
else
  "${GC[@]}" logging sinks create "$SINK" "$DESTINATION" --log-filter="$FILTER" --use-partitioned-tables \
    --description "Easytop ${ENVIRONMENT}: analytics events only"
fi

# 4. Let the sink's own service account write into this dataset only (not the whole project).
WRITER="$("${GC[@]}" logging sinks describe "$SINK" --format='value(writerIdentity)')"
WRITER_EMAIL="${WRITER#serviceAccount:}"
POLICY="$(mktemp)"
trap 'rm -f "$POLICY"' EXIT
bq --project_id "$PROJECT" show --format=prettyjson "${PROJECT}:${DATASET}" > "$POLICY"
if python3 - "$POLICY" "$WRITER_EMAIL" <<'PY'
import json, sys
path, email = sys.argv[1], sys.argv[2]
data = json.load(open(path))
access = data.setdefault("access", [])
if any(a.get("userByEmail") == email and a.get("role") in ("WRITER", "OWNER") for a in access):
    sys.exit(1)  # already there
access.append({"role": "WRITER", "userByEmail": email})
json.dump({"access": access}, open(path, "w"))
PY
then
  bq --project_id "$PROJECT" update --source "$POLICY" "${PROJECT}:${DATASET}"
  echo "sink writer ${WRITER_EMAIL} may now write to ${DATASET}"
else
  echo "sink writer already has access to ${DATASET}"
fi

echo "done: events of ${SERVICE} flow into ${PROJECT}:${DATASET} (table run_googleapis_com_stderr appears a few minutes after the first event)"
