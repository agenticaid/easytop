#!/usr/bin/env bash
# Build the image with Cloud Build and deploy it to Cloud Run as ONE always-on instance of an environment.
#   tools/ops/deploy_cloudrun.sh PROJECT_ID REGION dev|prod
# Environments (same project, same image, nothing shared at run time):
#   dev   service easytop-dev   account easytop-bot-dev   secrets NAME        (the first six secrets, EasyTop-Dev database)
#   prod  service easytop-prod  account easytop-bot-prod  secrets NAME_PROD   (EasyTop-Prod database, its own bot)
# Create the prod secrets first with tools/ops/create_secrets.sh. Secret values are never read here.
set -euo pipefail

PROJECT="${1:?usage: deploy_cloudrun.sh PROJECT_ID REGION dev|prod}"
REGION="${2:?usage: deploy_cloudrun.sh PROJECT_ID REGION dev|prod}"
ENVIRONMENT="${3:?usage: deploy_cloudrun.sh PROJECT_ID REGION dev|prod}"
case "$ENVIRONMENT" in
  dev)  SUFFIX="" ;;
  prod) SUFFIX="_PROD" ;;
  *) echo "environment must be dev or prod" >&2; exit 2 ;;
esac
# Branches follow environments (Erez 2026-10-11): `dev` is deployed to dev, `main` to prod. Work lands on `dev` first;
# main only gets what Erez has reviewed. EASYTOP_ALLOW_ANY_BRANCH=1 overrides this for a one-off.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
case "$ENVIRONMENT" in dev) WANT_BRANCH=dev ;; prod) WANT_BRANCH=main ;; esac
if [ "$BRANCH" != "$WANT_BRANCH" ] && [ -z "${EASYTOP_ALLOW_ANY_BRANCH:-}" ]; then
  echo "the $ENVIRONMENT environment is deployed from branch '$WANT_BRANCH', you are on '$BRANCH'" >&2
  exit 2
fi
SERVICE="easytop-${ENVIRONMENT}"
ACCOUNT="easytop-bot-${ENVIRONMENT}@${PROJECT}.iam.gserviceaccount.com"
MEMORY="${EASYTOP_MEMORY:-2Gi}"   # 2 GiB until a real login has been measured (plan T1)
GC=(gcloud --project "$PROJECT")

if [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)" ]; then
  echo "warning: this commit is not what GitHub has for '$BRANCH' (push first, so the deployed code is on GitHub)" >&2
fi
if ! git diff --quiet HEAD -- 2>/dev/null; then
  echo "warning: uncommitted changes are NOT in the image tag below, but they are in the build" >&2
fi
TAG="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/easytop/bot:${TAG}"

"${GC[@]}" builds submit --tag "$IMAGE" .

# The service address is predictable, so the first revision already knows where the login page lives. It is
# checked against the real address afterwards.
NUMBER="$("${GC[@]}" projects describe "$PROJECT" --format='value(projectNumber)')"
URL="https://${SERVICE}-${NUMBER}.${REGION}.run.app"

# Prod only (Erez 2026-10-13): at most this many questions per parent per Israel-time day, admins exempt. It lives
# here because --set-env-vars replaces every variable, so a hand-set value would vanish at the next deploy.
# EASYTOP_DAILY_CAP=N changes the number for one deploy, 0 turns the cap off. Dev has no cap.
DAILY_CAP_ENV=""
if [ "$ENVIRONMENT" = "prod" ]; then DAILY_CAP_ENV=",EASYTOP_DAILY_QUESTION_CAP=${EASYTOP_DAILY_CAP:-5}"; fi

SECRETS=""
for name in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_IDS ANTHROPIC_API_KEY SUPABASE_URL SUPABASE_SERVICE_KEY SESSION_ENCRYPTION_KEY; do
  SECRETS+="${SECRETS:+,}${name}=${name}${SUFFIX}:latest"
done

"${GC[@]}" run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$ACCOUNT" \
  --execution-environment gen2 \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling --cpu 1 --memory "$MEMORY" \
  --timeout 3600 --session-affinity \
  --allow-unauthenticated \
  --set-secrets "$SECRETS" \
  --set-env-vars "EASYTOP_CONNECT_URL=${URL},EASYTOP_ENV=${ENVIRONMENT}${DAILY_CAP_ENV}"

ACTUAL="$("${GC[@]}" run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
if [ "$ACTUAL" != "$URL" ]; then
  echo "service address differs from the guess, updating EASYTOP_CONNECT_URL" >&2
  "${GC[@]}" run services update "$SERVICE" --region "$REGION" --update-env-vars "EASYTOP_CONNECT_URL=${ACTUAL}"
fi
echo "deployed ${IMAGE} as ${SERVICE} at ${ACTUAL}"
