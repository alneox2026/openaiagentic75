#!/usr/bin/env bash
set -euo pipefail

# Build the two middleware images using a name unique to this repository's
# deployment stack. This prevents one middleware from overwriting another
# stack's mutable :latest tag in the shared Artifact Registry repository.

PROJECT_ID="${PROJECT_ID:-ceo-dev123}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-ceosystem}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIDDLEWARE_STACK_NAME="${MIDDLEWARE_STACK_NAME:-$(basename "${ROOT_DIR}")}"
MIDDLEWARE_IMAGE_PREFIX="${MIDDLEWARE_IMAGE_PREFIX:-${MIDDLEWARE_STACK_NAME}}"

if [[ ! "${MIDDLEWARE_IMAGE_PREFIX}" =~ ^[a-z][a-z0-9-]*$ ]]; then
  echo "ERROR: MIDDLEWARE_IMAGE_PREFIX must contain only lowercase letters, digits, and hyphens, and start with a letter." >&2
  exit 2
fi

echo "================================================================="
echo " Building Middleware Container Images"
echo " Project ID   : ${PROJECT_ID}"
echo " Region       : ${REGION}"
echo " Repository   : ${REPOSITORY}"
echo " Image Prefix : ${MIDDLEWARE_IMAGE_PREFIX}"
echo "================================================================="

gcloud config set project "${PROJECT_ID}"
cd "${ROOT_DIR}"

TAG="$(git rev-parse --short=12 HEAD 2>/dev/null || echo "middleware-$(date +%s)")"
echo "Build Tag: ${TAG}"

gcloud artifacts repositories describe "${REPOSITORY}" \
  --location="${REGION}" \
  --project="${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${REPOSITORY}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Middleware Docker Repository" \
  --project="${PROJECT_ID}"

GATEWAY_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${MIDDLEWARE_IMAGE_PREFIX}-gateway:${TAG}"
GATEWAY_LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${MIDDLEWARE_IMAGE_PREFIX}-gateway:latest"
WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${MIDDLEWARE_IMAGE_PREFIX}-persistence-worker:${TAG}"
WORKER_LATEST="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${MIDDLEWARE_IMAGE_PREFIX}-persistence-worker:latest"

echo "--> [1/2] Building Gateway Image: ${GATEWAY_IMAGE}"
gcloud builds submit . \
  --config=<(cat <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "services/agent_gateway_v3/Dockerfile", "-t", "${GATEWAY_IMAGE}", "-t", "${GATEWAY_LATEST}", "."]
images:
- "${GATEWAY_IMAGE}"
- "${GATEWAY_LATEST}"
EOF
) --project="${PROJECT_ID}"

echo "--> [2/2] Building Persistence Worker Image: ${WORKER_IMAGE}"
gcloud builds submit . \
  --config=<(cat <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "services/agent_persistence_worker_v3/Dockerfile", "-t", "${WORKER_IMAGE}", "-t", "${WORKER_LATEST}", "."]
images:
- "${WORKER_IMAGE}"
- "${WORKER_LATEST}"
EOF
) --project="${PROJECT_ID}"

echo "================================================================="
echo " Middleware Container Images Built & Pushed Successfully!"
echo " Gateway Image     : ${GATEWAY_IMAGE}"
echo " Worker Image      : ${WORKER_IMAGE}"
echo "================================================================="
