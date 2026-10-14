#!/usr/bin/env bash
# Let one more Telegram user into the cloud bot.
#   tools/ops/add_allowed_id.sh TELEGRAM_ID dev|prod [PROJECT_ID] [REGION]
# Appends the id to the TELEGRAM_ALLOWED_IDS secret (a new version, the old one stays) and starts a new revision so
# the bot reads it. Run it yourself (`! tools/ops/add_allowed_id.sh 123456789`): it writes a secret.
# Get a person's id: they message the cloud bot once (it answers "private beta"); the Cloud Run log then holds a line
# `denied parent=telegram:<id>`:
#   gcloud logging read 'jsonPayload.message=~"denied parent"' --limit 5 --freshness 1d --format='value(jsonPayload.message)'
# A restart drops an unfinished login on the phone page (links live in memory): do not run it while someone signs in.
set -euo pipefail

ID="${1:?usage: add_allowed_id.sh TELEGRAM_ID dev|prod [PROJECT_ID] [REGION]}"
ENVIRONMENT="${2:?usage: add_allowed_id.sh TELEGRAM_ID dev|prod [PROJECT_ID] [REGION]}"
PROJECT="${3:-your-gcp-project}"
REGION="${4:-me-west1}"
case "$ENVIRONMENT" in (dev) SUFFIX="";; (prod) SUFFIX="_PROD";; (*) echo "dev or prod" >&2; exit 2;; esac
SECRET="TELEGRAM_ALLOWED_IDS${SUFFIX}"
case "$ID" in (*[!0-9]*|"") echo "a Telegram id is digits only" >&2; exit 2;; esac
GC=(gcloud --configuration easytop --project "$PROJECT")

CURRENT="$("${GC[@]}" secrets versions access latest --secret "$SECRET")"
case ",${CURRENT// /}," in
  (*",${ID},"*) echo "id already allowed"; exit 0;;
esac
printf '%s,%s' "${CURRENT%,}" "$ID" | "${GC[@]}" secrets versions add "$SECRET" --data-file=- >/dev/null
echo "added; the list now has $(printf '%s,%s' "${CURRENT%,}" "$ID" | tr ',' '\n' | grep -c .) ids"

# secrets are read when an instance starts: a changed variable makes a new revision, which picks up :latest
"${GC[@]}" run services update "easytop-${ENVIRONMENT}" --region "$REGION" --update-env-vars "ALLOWLIST_REV=$(date +%s)" >/dev/null
echo "new revision started"
