#!/usr/bin/env bash
set -euo pipefail

# Recovery-only helper. Do not use this for a new stack: importing resources
# from another state makes Terraform propose destructive renames on the next
# apply.

if [ "${IMPORT_EXISTING_RESOURCES:-false}" != "true" ]; then
  echo "Refusing automatic import. Set IMPORT_EXISTING_RESOURCES=true only after verifying every resource belongs to this stack." >&2
  exit 2
fi

PROJECT_ID="${PROJECT_ID:-ceo-dev123}"
REGION="${REGION:-us-central1}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIDDLEWARE_STACK_NAME="${MIDDLEWARE_STACK_NAME:-$(basename "${ROOT_DIR}")}"

if [ -z "${MIDDLEWARE_RESOURCE_PREFIX:-}" ]; then
  if [ "${#MIDDLEWARE_STACK_NAME}" -le 15 ]; then
    MIDDLEWARE_RESOURCE_PREFIX="${MIDDLEWARE_STACK_NAME}"
  else
    STACK_HASH="$(printf '%s' "${MIDDLEWARE_STACK_NAME}" | sha256sum | cut -c1-8)"
    MIDDLEWARE_RESOURCE_PREFIX="${MIDDLEWARE_STACK_NAME:0:6}-${STACK_HASH}"
  fi
fi

# If the Terraform service-name defaults were customized, pass their exact
# values through these environment variables before recovering a lost state.
GATEWAY_SERVICE_NAME="${GATEWAY_SERVICE_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-gateway}"
WORKER_SERVICE_NAME="${WORKER_SERVICE_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-persistence-worker}"
GATEWAY_SERVICE_ACCOUNT_NAME="${GATEWAY_SERVICE_ACCOUNT_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-gateway-sa}"
WORKER_SERVICE_ACCOUNT_NAME="${WORKER_SERVICE_ACCOUNT_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-worker-sa}"
EVENTARC_SERVICE_ACCOUNT_NAME="${EVENTARC_SERVICE_ACCOUNT_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-eventarc-sa}"
BILLING_RECONCILER_SERVICE_ACCOUNT_NAME="${BILLING_RECONCILER_SERVICE_ACCOUNT_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-reconciler-sa}"
PUBSUB_TOPIC_NAME="${PUBSUB_TOPIC_NAME:-${MIDDLEWARE_RESOURCE_PREFIX}-turn-events}"

cd "${ROOT_DIR}/infra/terraform"

is_tracked() {
  terraform state list 2>/dev/null | grep -Fxq "$1"
}

import_if_exists() {
  local tf_resource="$1"
  local gcp_id="$2"
  shift 2

  if is_tracked "${tf_resource}"; then
    echo "    ${tf_resource} is already tracked in state."
    return
  fi

  if "$@" >/dev/null 2>&1; then
    echo "--> Importing ${tf_resource} (${gcp_id})..."
    terraform import "${tf_resource}" "${gcp_id}"
  else
    echo "    ${tf_resource} does not exist; Terraform will create it."
  fi
}

echo "================================================================="
echo " Importing resources for the verified ${MIDDLEWARE_STACK_NAME} stack"
echo " Project ID : ${PROJECT_ID}"
echo " Region     : ${REGION}"
echo "================================================================="

import_if_exists "google_service_account.gateway" "projects/${PROJECT_ID}/serviceAccounts/${GATEWAY_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  gcloud iam service-accounts describe "${GATEWAY_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --project="${PROJECT_ID}"
import_if_exists "google_service_account.worker" "projects/${PROJECT_ID}/serviceAccounts/${WORKER_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  gcloud iam service-accounts describe "${WORKER_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --project="${PROJECT_ID}"
import_if_exists "google_service_account.eventarc" "projects/${PROJECT_ID}/serviceAccounts/${EVENTARC_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  gcloud iam service-accounts describe "${EVENTARC_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --project="${PROJECT_ID}"
import_if_exists "google_service_account.billing_reconciler" "projects/${PROJECT_ID}/serviceAccounts/${BILLING_RECONCILER_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  gcloud iam service-accounts describe "${BILLING_RECONCILER_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --project="${PROJECT_ID}"

import_if_exists "google_pubsub_topic.agent_turn_events" "projects/${PROJECT_ID}/topics/${PUBSUB_TOPIC_NAME}" \
  gcloud pubsub topics describe "${PUBSUB_TOPIC_NAME}" --project="${PROJECT_ID}"
import_if_exists "google_cloud_run_v2_service.gateway" "projects/${PROJECT_ID}/locations/${REGION}/services/${GATEWAY_SERVICE_NAME}" \
  gcloud run services describe "${GATEWAY_SERVICE_NAME}" --region="${REGION}" --project="${PROJECT_ID}"
import_if_exists "google_cloud_run_v2_service.worker" "projects/${PROJECT_ID}/locations/${REGION}/services/${WORKER_SERVICE_NAME}" \
  gcloud run services describe "${WORKER_SERVICE_NAME}" --region="${REGION}" --project="${PROJECT_ID}"
import_if_exists "google_eventarc_trigger.worker_turn_events" "projects/${PROJECT_ID}/locations/${REGION}/triggers/${WORKER_SERVICE_NAME}-turn-events" \
  gcloud eventarc triggers describe "${WORKER_SERVICE_NAME}-turn-events" --location="${REGION}" --project="${PROJECT_ID}"
import_if_exists "google_cloud_scheduler_job.billing_reconciliation" "projects/${PROJECT_ID}/locations/${REGION}/jobs/${WORKER_SERVICE_NAME}-billing-reconciliation" \
  gcloud scheduler jobs describe "${WORKER_SERVICE_NAME}-billing-reconciliation" --location="${REGION}" --project="${PROJECT_ID}"
import_if_exists "google_logging_metric.worker_retryable_failures" "${WORKER_SERVICE_NAME}_retryable_failures" \
  gcloud logging metrics describe "${WORKER_SERVICE_NAME}_retryable_failures" --project="${PROJECT_ID}"

echo "--> Verified stack resource import check complete."
