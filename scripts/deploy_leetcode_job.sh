#!/usr/bin/env bash
set -euo pipefail

# Deploy the LeetCode solver as a Cloud Run Job.
# Runtime argument remains URL-only:
#   gcloud run jobs execute "$JOB_NAME" --region "$REGION" --args 'https://leetcode.com/problems/two-sum/' --wait

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Support both ./deploy_leetcode_job.sh and scripts/deploy_leetcode_job.sh.
if [[ -f "${SCRIPT_DIR}/Dockerfile" ]]; then
  ROOT_DIR="$SCRIPT_DIR"
elif [[ -f "${SCRIPT_DIR}/../Dockerfile" ]]; then
  ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  ROOT_DIR="$SCRIPT_DIR"
fi
cd "$ROOT_DIR"

log() { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

load_env_file() {
  local file="${1:-.env}"
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *"="* ]] || continue
    local key="${line%%=*}"
    local value="${line#*=}"
    key="$(printf '%s' "$key" | xargs)"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # Environment variables already exported by the caller win over .env.
    if [[ -n "${!key-}" ]]; then
      continue
    fi
    # Basic .env parsing: trim outer single/double quotes only. No command execution.
    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"
    if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$file"
}

secret_exists() {
  gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1
}

upsert_secret_from_file() {
  local name="$1"
  local file="$2"
  [[ -f "$file" ]] || fail "secret file not found: $file"
  if secret_exists "$name"; then
    log "adding new Secret Manager version: $name"
    gcloud secrets versions add "$name" --project "$PROJECT_ID" --data-file="$file" >/dev/null
  else
    log "creating Secret Manager secret: $name"
    gcloud secrets create "$name" --project "$PROJECT_ID" --replication-policy=automatic --data-file="$file" >/dev/null
  fi
}

upsert_secret_from_value() {
  local name="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  chmod 600 "$tmp"
  printf '%s' "$value" > "$tmp"
  upsert_secret_from_file "$name" "$tmp"
  rm -f "$tmp"
}

grant_secret_access() {
  local secret_name="$1"
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project "$PROJECT_ID" \
    --member "serviceAccount:${RUNNER_SA_EMAIL}" \
    --role roles/secretmanager.secretAccessor \
    --quiet >/dev/null
}

urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

maybe_create_atlas_cluster() {
  [[ "${ATLAS_SETUP:-false}" == "true" ]] || return 0
  have atlas || fail "ATLAS_SETUP=true requires the MongoDB Atlas CLI: https://www.mongodb.com/docs/atlas/cli/current/install-atlas-cli/"
  have python3 || fail "python3 is required to URL-encode Atlas credentials"

  : "${ATLAS_CLUSTER_NAME:=leetcode-solver}"
  : "${ATLAS_PROVIDER:=GCP}"
  : "${ATLAS_REGION:=CENTRAL_US}"
  : "${ATLAS_TIER:=M0}"
  : "${ATLAS_DB_USERNAME:=leetcode_solver}"
  if [[ -z "${ATLAS_DB_PASSWORD:-}" ]]; then
    have openssl || fail "ATLAS_DB_PASSWORD is not set and openssl is unavailable to generate one"
    ATLAS_DB_PASSWORD="$(openssl rand -base64 24 | tr -d '\n')"
    export ATLAS_DB_PASSWORD
  fi

  local project_args=()
  [[ -n "${ATLAS_PROJECT_ID:-}" ]] && project_args+=(--projectId "$ATLAS_PROJECT_ID")
  local access_args=()
  [[ -n "${ATLAS_ACCESS_LIST_IP:-}" ]] && access_args+=(--accessListIp "$ATLAS_ACCESS_LIST_IP")

  log "creating/configuring MongoDB Atlas cluster ${ATLAS_CLUSTER_NAME} on ${ATLAS_PROVIDER}/${ATLAS_REGION} (${ATLAS_TIER})"
  atlas setup \
    --clusterName "$ATLAS_CLUSTER_NAME" \
    --provider "$ATLAS_PROVIDER" \
    --region "$ATLAS_REGION" \
    --tier "$ATLAS_TIER" \
    --username "$ATLAS_DB_USERNAME" \
    --password "$ATLAS_DB_PASSWORD" \
    --skipSampleData \
    --connectWith skip \
    --force \
    "${project_args[@]}" \
    "${access_args[@]}"

  log "fetching Atlas SRV connection string"
  local srv enc_user enc_pass
  srv="$(atlas clusters connectionStrings describe "$ATLAS_CLUSTER_NAME" "${project_args[@]}" | awk '/mongodb\+srv:\/\// {print; exit}')"
  [[ -n "$srv" ]] || fail "could not read Atlas connection string for $ATLAS_CLUSTER_NAME"
  enc_user="$(urlencode "$ATLAS_DB_USERNAME")"
  enc_pass="$(urlencode "$ATLAS_DB_PASSWORD")"

  if [[ "$srv" == *"<username>"* || "$srv" == *"<password>"* ]]; then
    MONGODB_URI="${srv//<username>/$enc_user}"
    MONGODB_URI="${MONGODB_URI//<password>/$enc_pass}"
  elif [[ "$srv" == mongodb+srv://*"@"* ]]; then
    MONGODB_URI="$srv"
  else
    MONGODB_URI="${srv/mongodb+srv:\/\//mongodb+srv://${enc_user}:${enc_pass}@}"
  fi
  [[ "$MONGODB_URI" == *"/"*"?"* ]] || MONGODB_URI="${MONGODB_URI%/}/${MONGODB_DB:-leetcode_solver}?retryWrites=true&w=majority"
  export MONGODB_URI
  log "Atlas cluster ready; MONGODB_URI will be stored in Secret Manager"
}

load_env_file "${ENV_FILE:-.env}"

: "${PROJECT_ID:=${GOOGLE_CLOUD_PROJECT:-}}"
[[ -n "${PROJECT_ID:-}" ]] || fail "PROJECT_ID or GOOGLE_CLOUD_PROJECT is required. Create .env in repo root or export PROJECT_ID=..."
: "${REGION:=us-central1}"
: "${GOOGLE_CLOUD_LOCATION:=global}"
: "${JOB_NAME:=leetcode-solver-job}"
: "${AR_REPO:=leetcode-solver}"
: "${IMAGE_NAME:=lc-playwright-solver-py}"
: "${RUNNER_SA_NAME:=leetcode-solver-runner}"
: "${RUN_ROOT:=/tmp/leetcode-runs}"
: "${LC_AUTH_JSON_FILE:=lc-auth.json}"
: "${LC_AUTH_SECRET_NAME:=leetcode-lc-auth-json}"
: "${MONGODB_URI_SECRET_NAME:=leetcode-mongodb-uri}"
: "${OVERMIND_API_KEY_SECRET_NAME:=leetcode-overmind-api-key}"
: "${LC_LANG:=python3}"
: "${LC_LLM:=true}"
: "${LC_HEADLESS:=true}"
: "${LC_DRY_RUN:=false}"
: "${LC_VERBOSE_RESULT:=false}"
: "${MONGODB_DB:=leetcode_solver}"
: "${MONGODB_COLLECTION:=solution_packs}"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNNER_SA_EMAIL="${RUNNER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:latest"

maybe_create_atlas_cluster

log "enabling required Google APIs"
gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project "$PROJECT_ID" >/dev/null

if ! gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  log "creating Artifact Registry repo: $AR_REPO"
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location "$REGION" \
    --project "$PROJECT_ID" >/dev/null
fi

if ! gcloud iam service-accounts describe "$RUNNER_SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  log "creating service account: $RUNNER_SA_EMAIL"
  gcloud iam service-accounts create "$RUNNER_SA_NAME" \
    --project "$PROJECT_ID" \
    --display-name "LeetCode solver Cloud Run Job runner" >/dev/null
fi

log "granting runner IAM roles"
for role in roles/aiplatform.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${RUNNER_SA_EMAIL}" \
    --role "$role" \
    --quiet >/dev/null
done

upsert_secret_from_file "$LC_AUTH_SECRET_NAME" "$LC_AUTH_JSON_FILE"
grant_secret_access "$LC_AUTH_SECRET_NAME"

SECRET_SPECS="/secrets/lc-auth.json=${LC_AUTH_SECRET_NAME}:latest"

if [[ -n "${MONGODB_URI_FILE:-}" ]]; then
  upsert_secret_from_file "$MONGODB_URI_SECRET_NAME" "$MONGODB_URI_FILE"
  grant_secret_access "$MONGODB_URI_SECRET_NAME"
  SECRET_SPECS="${SECRET_SPECS},MONGODB_URI=${MONGODB_URI_SECRET_NAME}:latest"
elif [[ -n "${MONGODB_URI:-}" ]]; then
  upsert_secret_from_value "$MONGODB_URI_SECRET_NAME" "$MONGODB_URI"
  grant_secret_access "$MONGODB_URI_SECRET_NAME"
  SECRET_SPECS="${SECRET_SPECS},MONGODB_URI=${MONGODB_URI_SECRET_NAME}:latest"
else
  log "MONGODB_URI/MONGODB_URI_FILE not set; MongoDB persistence will stay disabled"
fi

if [[ -n "${OVERMIND_API_KEY_FILE:-}" ]]; then
  upsert_secret_from_file "$OVERMIND_API_KEY_SECRET_NAME" "$OVERMIND_API_KEY_FILE"
  grant_secret_access "$OVERMIND_API_KEY_SECRET_NAME"
  SECRET_SPECS="${SECRET_SPECS},OVERMIND_API_KEY=${OVERMIND_API_KEY_SECRET_NAME}:latest"
elif [[ -n "${OVERMIND_API_KEY:-}" ]]; then
  upsert_secret_from_value "$OVERMIND_API_KEY_SECRET_NAME" "$OVERMIND_API_KEY"
  grant_secret_access "$OVERMIND_API_KEY_SECRET_NAME"
  SECRET_SPECS="${SECRET_SPECS},OVERMIND_API_KEY=${OVERMIND_API_KEY_SECRET_NAME}:latest"
fi

ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-True},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},RUN_ROOT=${RUN_ROOT},LC_AUTH_PATH=/secrets/lc-auth.json,LC_LANG=${LC_LANG},LC_LLM=${LC_LLM},LC_HEADLESS=${LC_HEADLESS},LC_DRY_RUN=${LC_DRY_RUN},LC_VERBOSE_RESULT=${LC_VERBOSE_RESULT},MONGODB_DB=${MONGODB_DB},MONGODB_COLLECTION=${MONGODB_COLLECTION}"
[[ -n "${LEETCODE_CODE_MODEL:-}" ]] && ENV_VARS="${ENV_VARS},LEETCODE_CODE_MODEL=${LEETCODE_CODE_MODEL}"
[[ -n "${LEETCODE_RATIONALE_MODEL:-}" ]] && ENV_VARS="${ENV_VARS},LEETCODE_RATIONALE_MODEL=${LEETCODE_RATIONALE_MODEL}"
[[ -n "${OUTPUT_GCS_URI:-}" ]] && ENV_VARS="${ENV_VARS},OUTPUT_GCS_URI=${OUTPUT_GCS_URI}"
[[ -n "${OVERMIND_SERVICE_NAME:-}" ]] && ENV_VARS="${ENV_VARS},OVERMIND_SERVICE_NAME=${OVERMIND_SERVICE_NAME}"
[[ -n "${OVERMIND_ENVIRONMENT:-}" ]] && ENV_VARS="${ENV_VARS},OVERMIND_ENVIRONMENT=${OVERMIND_ENVIRONMENT}"
[[ -n "${OVERMIND_ENABLED:-}" ]] && ENV_VARS="${ENV_VARS},OVERMIND_ENABLED=${OVERMIND_ENABLED}"

log "building image: $IMAGE_URI"
gcloud builds submit --project "$PROJECT_ID" --tag "$IMAGE_URI" .

if gcloud run jobs describe "$JOB_NAME" --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  log "updating Cloud Run Job: $JOB_NAME"
  gcloud run jobs update "$JOB_NAME" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE_URI" \
    --service-account "$RUNNER_SA_EMAIL" \
    --set-env-vars "$ENV_VARS" \
    --set-secrets "$SECRET_SPECS" \
    --task-timeout "${TASK_TIMEOUT:=1800s}" \
    --memory "${JOB_MEMORY:=2Gi}" \
    --cpu "${JOB_CPU:=2}" >/dev/null
else
  log "creating Cloud Run Job: $JOB_NAME"
  gcloud run jobs create "$JOB_NAME" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE_URI" \
    --service-account "$RUNNER_SA_EMAIL" \
    --set-env-vars "$ENV_VARS" \
    --set-secrets "$SECRET_SPECS" \
    --task-timeout "${TASK_TIMEOUT:=1800s}" \
    --memory "${JOB_MEMORY:=2Gi}" \
    --cpu "${JOB_CPU:=2}" >/dev/null
fi

log "deploy complete"
log "execute with: gcloud run jobs execute ${JOB_NAME} --project ${PROJECT_ID} --region ${REGION} --args 'https://leetcode.com/problems/two-sum/' --wait"
