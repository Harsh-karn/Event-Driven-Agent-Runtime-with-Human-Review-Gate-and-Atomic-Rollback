# ==============================================================================
# Tether Identity and Access Management (Least Privilege Architecture)
# ==============================================================================

# 1. Cloud Run Worker Service Account
resource "google_service_account" "worker" {
  account_id   = "tether-worker-sa"
  display_name = "Tether Worker Service Account"
  description  = "Dedicated identity for Tether background queue workers and outbox export"
}

# 2. Cloud Run API Service Account
resource "google_service_account" "api" {
  account_id   = "tether-api-sa"
  display_name = "Tether API Service Account"
  description  = "Dedicated identity for Tether ingestion and review gate endpoints"
}

# 3. Cloud Scheduler Invoker Service Account
resource "google_service_account" "scheduler" {
  account_id   = "tether-scheduler-sa"
  display_name = "Tether Scheduler Invoker"
  description  = "Invokes Tether API run endpoints with authenticated OIDC identity tokens"
}

# ------------------------------------------------------------------------------
# Secret Manager Access (Worker & API)
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "worker_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "api_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# ------------------------------------------------------------------------------
# BigQuery Permissions (Worker Only - Data Editor scoped to Tether Dataset)
# ------------------------------------------------------------------------------
resource "google_bigquery_dataset_iam_member" "worker_bq_data_editor" {
  dataset_id = google_bigquery_dataset.warehouse.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "worker_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

# ------------------------------------------------------------------------------
# Cloud Run Invocation Permissions (Scheduler -> API)
# ------------------------------------------------------------------------------
resource "google_cloud_run_service_iam_member" "scheduler_invoker" {
  location = var.region
  service  = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}
