resource "google_service_account" "gateway" {
  account_id   = var.gateway_service_account_name
  display_name = "CEOsystem Agent Gateway"
}

resource "google_service_account" "worker" {
  account_id   = var.worker_service_account_name
  display_name = "CEOsystem Agent Persistence Worker"
}

resource "google_service_account" "eventarc" {
  account_id   = var.eventarc_service_account_name
  display_name = "CEOsystem Eventarc Trigger"
}

resource "google_service_account" "billing_reconciler" {
  account_id   = var.billing_reconciler_service_account_name
  display_name = "CEOsystem Billing Reconciler"
}

locals {
  gateway_roles = toset([
    "roles/cloudtrace.agent",
    "roles/datastore.user",
    "roles/logging.logWriter",
    "roles/pubsub.publisher",
  ])

  worker_roles = toset([
    "roles/cloudtrace.agent",
    "roles/datastore.user",
    "roles/logging.logWriter",
  ])

  eventarc_roles = toset([
    "roles/eventarc.eventReceiver",
    "roles/logging.logWriter",
  ])
}

resource "google_project_iam_member" "gateway_roles" {
  for_each = local.gateway_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = local.worker_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "eventarc_roles" {
  for_each = local.eventarc_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.eventarc.email}"
}

# The gateway needs access to the OpenAI API key for the managed Agents API.
resource "google_secret_manager_secret_iam_member" "gateway_openai_api_key_accessor" {
  count = var.openai_api_key_secret_id == "" ? 0 : 1

  project   = var.project_id
  secret_id = var.openai_api_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

# The worker reads the key only to recover missing token usage after a response.
resource "google_secret_manager_secret_iam_member" "worker_openai_api_key_accessor" {
  count = var.openai_api_key_secret_id == "" ? 0 : 1

  project   = var.project_id
  secret_id = var.openai_api_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}
