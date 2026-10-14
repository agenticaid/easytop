#!/usr/bin/env bash
# Create the PROD environment's six secrets in Google Secret Manager from values in .env. Nothing is printed.
#   tools/ops/create_secrets.sh PROJECT_ID prod
# Run it yourself (`! tools/ops/create_secrets.sh your-gcp-project prod`): it writes secrets, so Claude does not.
# The dev secrets (no suffix) already exist; this script refuses to touch them.
#
# Put these in .env first (all different from the dev ones):
#   TELEGRAM_BOT_TOKEN_CLOUD_PROD   the token of @your_prod_bot (BotFather); the dev bot is @your_dev_bot
#   SUPABASE_URL_PROD               https://<ref>.supabase.co of EasyTop-Prod (the API address, not the dashboard)
#   SUPABASE_SERVICE_KEY_PROD       its service key
#   ANTHROPIC_API_KEY_PROD          a separate Anthropic API key for prod, so the Console splits prod and dev spend
# TELEGRAM_ALLOWED_IDS is reused from .env. SESSION_ENCRYPTION_KEY_PROD is generated here and kept
# only in Secret Manager, and never replaced (a new key would make every stored session unreadable).
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT="${1:?usage: create_secrets.sh PROJECT_ID prod}"
[ "${2:-}" = "prod" ] || { echo "only 'prod' is supported: the dev secrets already exist" >&2; exit 2; }
ACCOUNT="easytop-bot-prod@${PROJECT}.iam.gserviceaccount.com"
GC=(gcloud --configuration easytop --project "$PROJECT")

exists() { "${GC[@]}" secrets describe "$1" >/dev/null 2>&1; }
grant() { "${GC[@]}" secrets add-iam-policy-binding "$1" --member "serviceAccount:${ACCOUNT}" --role roles/secretmanager.secretAccessor --condition=None >/dev/null; }

# put SECRET_NAME ENV_VAR: create the secret (or add a version), and let the prod account read it
put() {
  local name="$1" var="$2"
  local value_cmd=(uv run --env-file .env python -c "import os,sys; sys.stdout.write(os.environ['$var'])")
  if exists "$name"; then
    "${value_cmd[@]}" | "${GC[@]}" secrets versions add "$name" --data-file=- >/dev/null && echo "updated $name"
  else
    "${value_cmd[@]}" | "${GC[@]}" secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null && echo "created $name"
  fi
  grant "$name"
}

put TELEGRAM_BOT_TOKEN_PROD TELEGRAM_BOT_TOKEN_CLOUD_PROD
put TELEGRAM_ALLOWED_IDS_PROD TELEGRAM_ALLOWED_IDS
put ANTHROPIC_API_KEY_PROD ANTHROPIC_API_KEY_PROD
put SUPABASE_URL_PROD SUPABASE_URL_PROD
put SUPABASE_SERVICE_KEY_PROD SUPABASE_SERVICE_KEY_PROD

if exists SESSION_ENCRYPTION_KEY_PROD; then
  echo "kept SESSION_ENCRYPTION_KEY_PROD (never replaced)"
else
  uv run python -c "import sys; from easytop.onboarding.crypto import new_key; sys.stdout.write(new_key().decode())" \
    | "${GC[@]}" secrets create SESSION_ENCRYPTION_KEY_PROD --data-file=- --replication-policy=automatic >/dev/null \
    && echo "created SESSION_ENCRYPTION_KEY_PROD (a new key, kept only in Secret Manager)"
fi
grant SESSION_ENCRYPTION_KEY_PROD

echo; "${GC[@]}" secrets list --format='value(name)' | grep _PROD
