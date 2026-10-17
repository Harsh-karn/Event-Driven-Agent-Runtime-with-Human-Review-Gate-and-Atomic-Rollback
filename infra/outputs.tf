# ==============================================================================
# Infrastructure Outputs
# ==============================================================================

output "api_service_url" {
  description = "Public URL for the Tether API service."
  value       = google_cloud_run_v2_service.api.uri
}

output "worker_service_name" {
  description = "Name of the Tether background queue worker Cloud Run service."
  value       = google_cloud_run_v2_service.worker.name
}

output "bigquery_dataset_id" {
  description = "BigQuery dataset ID for the Tether analytics warehouse."
  value       = google_bigquery_dataset.warehouse.dataset_id
}

output "bigquery_events_table" {
  description = "Fully qualified BigQuery table ID for partitioned event logging."
  value       = "${google_bigquery_dataset.warehouse.dataset_id}.${google_bigquery_table.events.table_id}"
}

output "worker_service_account_email" {
  description = "Email of the dedicated worker service account."
  value       = google_service_account.worker.email
}

output "api_service_account_email" {
  description = "Email of the dedicated API service account."
  value       = google_service_account.api.email
}

output "scheduler_job_name" {
  description = "Name of the scheduled cron trigger job."
  value       = google_cloud_scheduler_job.scheduled_digest.name
}
