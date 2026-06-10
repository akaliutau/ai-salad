#!/usr/bin/env bash
set -euo pipefail

# Cloud Run Job deploy for the LeetCode Gemini submitter.
#
# Design goal:
#   The deployed job has all fixed settings baked in through env vars/secrets.
#   At execution time, the ONLY custom parameter is the problem URL:
#
#     gcloud run jobs execute "$JOB_NAME" \
#       --region "$REGION" \
#       --args 'https://leetcode.com/problems/two-sum/' \
#       --wait
#
# This script intentionally deploys only a Cloud Run Job, not a public service/UI.

load_dotenv_file() {
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

create_or_update_secret_from_file_or_value() {
  local secret_name="$1"
  local file_var_name="$2"
  local value_var_name="$3"
  local file_value="${!file_var_name-}"
  local raw_value="${!value_var_name-}"

  if [[ -n "$file_value" ]]; then
    if [[ ! -f "$file_value" ]]; then
      if secret_exists "$secret_name"; then
        printf '\n[secret] %s is set but file does not exist: %s; reusing existing Secret Manager secret %s\n' \
          "$file_var_name" "$file_value" "$secret_name" >&2
        return 0
      fi
      printf '\n[secret] %s is set but file does not exist: %s\n' "$file_var_name" "$file_value" >&2
      return 1
    fi
    if secret_exists "$secret_name"; then
      printf '\n[secret] Adding a new version to Secret Manager secret %s from %s\n' "$secret_name" "$file_value"
      gcloud secrets versions add "$secret_name" \
        --data-file="$file_value" \
        --project "$PROJECT_ID" >/dev/null
    else
      printf '\n[secret] Creating Secret Manager secret %s from %s\n' "$secret_name" "$file_value"
      gcloud secrets create "$secret_name" \
        --replication-policy="automatic" \
        --data-file="$file_value" \
        --project "$PROJECT_ID" >/dev/null
    fi
    return 0
  fi

  if [[ -n "$raw_value" ]]; then
    local tmp
    tmp="$(mktemp)"
    chmod 600 "$tmp"
    printf '%s' "$raw_value" > "$tmp"
    if secret_exists "$secret_name"; then
      printf '\n[secret] Adding a new version to Secret Manager secret %s from %s\n' "$secret_name" "$value_var_name"
      gcloud secrets versions add "$secret_name" \
        --data-file="$tmp" \
        --project "$PROJECT_ID" >/dev/null
    else
      printf '\n[secret] Creating Secret Manager secret %s from %s\n' "$secret_name" "$value_var_name"
      gcloud secrets create "$secret_name" \
        --replication-policy="automatic" \
        --data-file="$tmp" \
        --project "$PROJECT_ID" >/dev/null
    fi
    rm -f "$tmp"
    unset "$value_var_name" raw_value
    return 0
  fi

  if secret_exists "$secret_name"; then
    printf '\n[secret] Reusing existing Secret Manager secret %s\n' "$secret_name"
    return 0
  fi

  printf '\n[secret] No data provided for %s and secret %s does not exist.\n' "$value_var_name" "$secret_name" >&2
  return 1
}

csv_join() {
  local IFS=,
  printf '%s' "$*"
}

maybe_set_adc_quota_project() {
  if [[ "${ADC_SET_QUOTA_PROJECT}" != "true" ]]; then
    return 0
  fi

  # Fixes the local warning: "active project does not match the quota project in ADC".
  # This is best-effort because it requires serviceusage.services.use on PROJECT_ID.
  if gcloud auth application-default set-quota-project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '\n[1/7] ADC quota project set to %s\n' "$PROJECT_ID"
  else
    printf '\n[1/7] WARNING: Could not set ADC quota project to %s. Continuing because gcloud CLI auth may still work.\n' "$PROJECT_ID" >&2
    printf '        Run manually if needed: gcloud auth application-default set-quota-project %q\n' "$PROJECT_ID" >&2
  fi
}

ensure_project_billing_enabled() {
  printf '\n[1/7] Checking Cloud Billing status for project %s\n' "$PROJECT_ID"

  local billing_enabled
  billing_enabled="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"

  if [[ "$billing_enabled" == "True" || "$billing_enabled" == "true" ]]; then
    printf '[1/7] Billing is enabled for %s\n' "$PROJECT_ID"
    return 0
  fi

  if [[ -n "$BILLING_ACCOUNT_ID" ]]; then
    printf '[1/7] Billing is not enabled. Attempting to link billing account %s\n' "$BILLING_ACCOUNT_ID"
    gcloud billing projects link "$PROJECT_ID" \
      --billing-account "$BILLING_ACCOUNT_ID"

    billing_enabled="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
    if [[ "$billing_enabled" == "True" || "$billing_enabled" == "true" ]]; then
      printf '[1/7] Billing is now enabled for %s\n' "$PROJECT_ID"
      return 0
    fi
  fi

  printf '\nERROR: Billing is not enabled for project %s.\n' "$PROJECT_ID" >&2
  printf 'This deploy creates billable resources: Cloud Storage buckets, Cloud Build, Artifact Registry, Cloud Run, and Vertex AI.\n' >&2
  printf '\nFix one of these ways:\n' >&2
  printf '  1) List billing accounts: gcloud billing accounts list\n' >&2
  printf '  2) Link one manually:   gcloud billing projects link %q --billing-account BILLING_ACCOUNT_ID\n' "$PROJECT_ID" >&2
  printf '  3) Or set BILLING_ACCOUNT_ID=... in .env and rerun this script.\n' >&2
  exit 2
}

load_dotenv_file .env

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID in .env or environment}"
REGION="${REGION:-us-central1}"
JOB_NAME="${JOB_NAME:-leetcode-solver-job}"
REPOSITORY="${REPOSITORY:-leetcode-solver}"
IMAGE_NAME="${IMAGE_NAME:-leetcode-solver}"
TAG="${TAG:-$(date +%Y%m%d%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:${TAG}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-leetcode-solver-runner}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com}"

# Optional: set BILLING_ACCOUNT_ID in .env to auto-link billing before billable resources are created.
BILLING_ACCOUNT_ID="${BILLING_ACCOUNT_ID:-}"
ADC_SET_QUOTA_PROJECT="${ADC_SET_QUOTA_PROJECT:-true}"

BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-leetcode-solver-runs}"
OUTPUT_GCS_URI="${OUTPUT_GCS_URI:-gs://${BUCKET_NAME}}"

LC_AUTH_SECRET="${LC_AUTH_SECRET:-leetcode-lc-auth-json}"
LC_AUTH_MOUNT_PATH="${LC_AUTH_MOUNT_PATH:-/secrets/lc-auth.json}"
LC_LANG="${LC_LANG:-python3}"
LC_LLM="${LC_LLM:-true}"
LC_HEADLESS="${LC_HEADLESS:-true}"
LC_DRY_RUN="${LC_DRY_RUN:-false}"
LC_TIMEOUT_MS="${LC_TIMEOUT_MS:-120000}"
RUN_ROOT="${RUN_ROOT:-/tmp/leetcode-runs}"

GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI:-True}"
GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-$PROJECT_ID}"
GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
VERTEX_LOCATION="${VERTEX_LOCATION:-$GOOGLE_CLOUD_LOCATION}"
LEETCODE_CODE_MODEL="${LEETCODE_CODE_MODEL:-${STAGE1_MODEL:-gemini-2.5-flash}}"
LEETCODE_RATIONALE_MODEL="${LEETCODE_RATIONALE_MODEL:-${STAGE1_MODEL:-$LEETCODE_CODE_MODEL}}"

if [[ "${GOOGLE_GENAI_USE_VERTEXAI}" =~ ^([Tt]rue|TRUE|1|yes|YES)$ && "${GOOGLE_CLOUD_PROJECT}" != "${PROJECT_ID}" ]]; then
  printf '\n[config] WARNING: GOOGLE_CLOUD_PROJECT=%s differs from PROJECT_ID=%s. Vertex AI calls from the job will use GOOGLE_CLOUD_PROJECT.\n' \
    "$GOOGLE_CLOUD_PROJECT" "$PROJECT_ID" >&2
fi

printf '\n[1/7] Configuring gcloud project %s\n' "$PROJECT_ID"
gcloud config set project "$PROJECT_ID" >/dev/null
maybe_set_adc_quota_project
ensure_project_billing_enabled

printf '\n[2/7] Enabling required APIs\n'
gcloud services enable \
  aiplatform.googleapis.com \
  compute.googleapis.com \
  logging.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  cloudbuild.googleapis.com \
  cloudbilling.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  run.googleapis.com \
  storage.googleapis.com \
  --project "$PROJECT_ID"

printf '\n[3/7] Ensuring Artifact Registry repo exists\n'
gcloud artifacts repositories create "$REPOSITORY" \
  --repository-format=docker \
  --location="$REGION" \
  --description="LeetCode Gemini submitter images" \
  --project "$PROJECT_ID" >/dev/null 2>&1 || true

printf '\n[4/7] Ensuring service account and IAM\n'
gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
  --display-name="LeetCode Gemini solver runner" \
  --project "$PROJECT_ID" >/dev/null 2>&1 || true

sleep 5

printf '\n[4/7] Ensuring GCS bucket exists: gs://%s\n' "$BUCKET_NAME"

if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" \
    --location="$REGION" \
    --project="$PROJECT_ID"
  sleep 5
fi

# Fail early with a useful message instead of hiding bucket creation errors.
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "ERROR: Bucket gs://${BUCKET_NAME} still does not exist."
  echo "Cloud Storage bucket names are globally unique."
  echo "Try a unique name, for example:"
  echo "  BUCKET_NAME=${PROJECT_ID}-leetcode-solver-runs"
  exit 1
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/aiplatform.user" \
  --condition=None >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/logging.logWriter" \
  --condition=None >/dev/null


gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin" \
  --project="$PROJECT_ID" >/dev/null

DEPLOYER_ACCOUNT="${DEPLOYER_ACCOUNT:-$(gcloud config get-value account 2>/dev/null)}"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${DEPLOYER_ACCOUNT}" \
  --role="roles/serviceusage.serviceUsageConsumer" \
  --condition=None >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${DEPLOYER_ACCOUNT}" \
  --role="roles/cloudbuild.builds.editor" \
  --condition=None >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${DEPLOYER_ACCOUNT}" \
  --role="roles/storage.admin" \
  --condition=None >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:${DEPLOYER_ACCOUNT}" \
  --role="roles/run.developer" \
  --condition=None >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$SERVICE_ACCOUNT" \
  --member="user:${DEPLOYER_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --project "$PROJECT_ID" >/dev/null


printf '\n[5/7] Creating/updating LeetCode auth storage-state secret\n'
create_or_update_secret_from_file_or_value "$LC_AUTH_SECRET" "LC_AUTH_JSON_FILE" "LC_AUTH_JSON"

gcloud secrets add-iam-policy-binding "$LC_AUTH_SECRET" \
  --project "$PROJECT_ID" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

printf '\n[6/7] Building image %s\n' "$IMAGE"

BUILD_BUCKET="${BUILD_BUCKET:-${PROJECT_ID}-leetcode-cloudbuild}"
BUILD_SOURCE_DIR="gs://${BUILD_BUCKET}/source"
BUILD_LOG_DIR="gs://${BUILD_BUCKET}/logs"

printf '[6/7] Ensuring Cloud Build staging bucket exists: gs://%s\n' "$BUILD_BUCKET"

if ! gcloud storage buckets describe "gs://${BUILD_BUCKET}" \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUILD_BUCKET}" \
    --location="$REGION" \
    --project="$PROJECT_ID"
fi

gcloud storage buckets add-iam-policy-binding "gs://${BUILD_BUCKET}" \
  --member="user:$(gcloud config get-value account 2>/dev/null)" \
  --role="roles/storage.objectAdmin" \
  --project="$PROJECT_ID" >/dev/null || true

# Keep every continued line as a real argument. Do not put commented-out
# arguments inside this backslash-continued command; bash would terminate the
# command at the comment and try to execute the next flag as its own command.
gcloud builds submit \
  --tag "$IMAGE" \
  --project "$PROJECT_ID" \
  .

printf '\n[7/7] Deploying Cloud Run Job %s\n' "$JOB_NAME"
JOB_ENV_VALUES=(
  "PROJECT_ID=${PROJECT_ID}"
  "GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI}"
  "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}"
  "GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION}"
  "VERTEX_LOCATION=${VERTEX_LOCATION}"
  "LEETCODE_CODE_MODEL=${LEETCODE_CODE_MODEL}"
  "LEETCODE_RATIONALE_MODEL=${LEETCODE_RATIONALE_MODEL}"
  "LC_AUTH_PATH=${LC_AUTH_MOUNT_PATH}"
  "LC_LANG=${LC_LANG}"
  "LC_LLM=${LC_LLM}"
  "LC_HEADLESS=${LC_HEADLESS}"
  "LC_DRY_RUN=${LC_DRY_RUN}"
  "LC_TIMEOUT_MS=${LC_TIMEOUT_MS}"
  "RUN_ROOT=${RUN_ROOT}"
  "OUTPUT_GCS_URI=${OUTPUT_GCS_URI}"
)
JOB_ENV="$(csv_join "${JOB_ENV_VALUES[@]}")"

# No --command or fixed --args here: the Dockerfile ENTRYPOINT is cloud_run_job.py.
# Therefore execution-time --args can be exactly one thing: the LeetCode problem URL.
gcloud run jobs deploy "$JOB_NAME" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SERVICE_ACCOUNT" \
  --tasks 1 \
  --max-retries 0 \
  --memory "${JOB_MEMORY:-2Gi}" \
  --cpu "${JOB_CPU:-2}" \
  --task-timeout "${JOB_TASK_TIMEOUT:-1800s}" \
  --set-env-vars "$JOB_ENV" \
  --set-secrets "${LC_AUTH_MOUNT_PATH}=${LC_AUTH_SECRET}:latest" \
  --project "$PROJECT_ID"

printf '\nDeploy complete. Execute with only the problem URL as the custom arg:\n\n'
printf '  gcloud run jobs execute %q --region %q --args %q --wait --project %q\n\n' \
  "$JOB_NAME" "$REGION" "https://leetcode.com/problems/two-sum/" "$PROJECT_ID"
printf 'Artifacts will be written under %s/<run-folder>/ when OUTPUT_GCS_URI is enabled.\n' "$OUTPUT_GCS_URI"
