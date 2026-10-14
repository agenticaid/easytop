#!/usr/bin/env bash
# Take one Telegram user out of the cloud bot's environment allowlist (the TELEGRAM_ALLOWED_IDS secret).
#   tools/ops/remove_allowed_id.sh TELEGRAM_ID dev|prod [PROJECT_ID] [REGION]
# Writes a new version of the secret without that id (the old one stays) and starts a new revision so the bot reads it.
# Run it yourself (`! tools/ops/remove_allowed_id.sh 123456789 dev`): it writes a secret.
# Refuses to leave the list empty. People let in by an invite link are in the database table allowed_users, not here:
# this script does not touch them. A restart drops an unfinished login on the phone page: not while someone signs in.
set -euo pipefail

ID="${1:?usage: remove_allowed_id.sh TELEGRAM_ID dev|prod [PROJECT_ID] [REGION]}"
ENVIRONMENT="${2:?usage: remove_allowed_id.sh TELEGRAM_ID dev|prod [PROJECT_ID] [REGION]}"
PROJECT="${3:-your-gcp-project}"
REGION="${4:-me-west1}"
case "$ENVIRONMENT" in (dev) SUFFIX="";; (prod) SUFFIX="_PROD";; (*) echo "dev or prod" >&2; exit 2;; esac
SECRET="TELEGRAM_ALLOWED_IDS${SUFFIX}"
case "$ID" in (*[!0-9]*|"") echo "a Telegram id is digits only" >&2; exit 2;; esac
GC=(gcloud --configuration easytop --project "$PROJECT")

CURRENT="$("${GC[@]}" secrets versions access latest --secret "$SECRET")"
case ",${CURRENT// /}," in
  (*",${ID},"*) ;;
  (*) echo "id is not in the list: nothing changed"; exit 0;;
esac
REMAINING="$(printf '%s' "${CURRENT// /}" | tr ',' '\n' | grep -vx "$ID" | paste -sd, - || true)"
[ -n "$REMAINING" ] || { echo "that would leave the list empty: nothing changed" >&2; exit 1; }
printf '%s' "$REMAINING" | "${GC[@]}" secrets versions add "$SECRET" --data-file=- >/dev/null
echo "removed; the list now has $(printf '%s' "$REMAINING" | tr ',' '\n' | grep -c .) id(s)"

# secrets are read when an instance starts: a changed variable makes a new revision, which picks up :latest
"${GC[@]}" run services update "easytop-${ENVIRONMENT}" --region "$REGION" --update-env-vars "ALLOWLIST_REV=$(date +%s)" >/dev/null
echo "new revision started"
