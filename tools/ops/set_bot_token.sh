#!/usr/bin/env bash
# Store a Telegram bot token as the new version of one environment's token secret. Nothing secret is printed.
#   tools/ops/set_bot_token.sh PROJECT_ID dev|prod
# Run it yourself (`! tools/ops/set_bot_token.sh your-gcp-project dev`): it writes a secret, so Claude does not.
#
# One bot per environment, each tied to its own database:
#   dev   .env TELEGRAM_BOT_TOKEN_CLOUD_DEV   must be @your_dev_bot    secret TELEGRAM_BOT_TOKEN       service easytop-dev
#   prod  .env TELEGRAM_BOT_TOKEN_CLOUD_PROD  must be @your_prod_bot   secret TELEGRAM_BOT_TOKEN_PROD  service easytop-prod
# The token is checked with Telegram's getMe first and must belong to the expected bot, so a dev token can never
# land in the prod secret (or the other way round). The prod secret must exist (tools/ops/create_secrets.sh).
# A running service picks the new version up at its next start: redeploy or restart it afterwards.
set -euo pipefail
cd "$(dirname "$0")/../.."

PROJECT="${1:?usage: set_bot_token.sh PROJECT_ID dev|prod}"
case "${2:-}" in
  dev)  VAR=TELEGRAM_BOT_TOKEN_CLOUD_DEV;  SECRET=TELEGRAM_BOT_TOKEN;      EXPECTED=your_dev_bot ;;
  prod) VAR=TELEGRAM_BOT_TOKEN_CLOUD_PROD; SECRET=TELEGRAM_BOT_TOKEN_PROD; EXPECTED=your_prod_bot ;;
  *) echo "usage: set_bot_token.sh PROJECT_ID dev|prod" >&2; exit 2 ;;
esac
GC=(gcloud --configuration easytop --project "$PROJECT")

uv run --env-file .env python -c "
import os, sys, httpx
token = os.environ.get('$VAR', '')
if not token:
    sys.exit('$VAR is not set in .env')
try:
    r = httpx.get(f'https://api.telegram.org/bot{token}/getMe', timeout=15)
    name = r.json()['result']['username'] if r.status_code == 200 else None
except Exception as e:  # the error text can hold the token: say only the type
    sys.exit(f'getMe failed: {type(e).__name__}')
if name is None:
    sys.exit('Telegram did not accept $VAR (getMe failed)')
if name.lower() != '$EXPECTED':
    sys.exit(f'$VAR belongs to @{name}, expected @$EXPECTED: nothing stored')
print(f'$VAR is @{name}: ok')
"

"${GC[@]}" secrets describe "$SECRET" >/dev/null 2>&1 \
  || { echo "secret $SECRET does not exist yet (prod: run tools/ops/create_secrets.sh first)" >&2; exit 1; }
uv run --env-file .env python -c "import os,sys; sys.stdout.write(os.environ['$VAR'])" \
  | "${GC[@]}" secrets versions add "$SECRET" --data-file=- >/dev/null
echo "stored a new version of $SECRET for @$EXPECTED; restart the $2 service to use it"
