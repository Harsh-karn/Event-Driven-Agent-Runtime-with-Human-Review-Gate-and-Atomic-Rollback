# ==============================================================================
# Cloud Scheduler: Periodic Event Triggers for Scheduled Agents
# ==============================================================================

resource "google_cloud_scheduler_job" "scheduled_digest" {
  name             = "tether-daily-digest-trigger"
  description      = "Fires scheduled cron events into Tether API to trigger autonomous digest workflows"
  schedule         = var.cron_schedule
  time_zone        = "UTC"
  attempt_deadline = "300s"

  retry_config {
    retry_count          = 3
    min_backoff_duration = "5s"
    max_backoff_duration = "60s"
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/runs"

    headers = {
      "Content-Type" = "application/json"
    }

    body = base64encode(jsonencode({
      agent_id = "demo_digest"
      trigger  = "schedule"
      payload = {
        source  = "gcp_cloud_scheduler"
        cadence = "daily"
      }
    }))

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service.api,
    google_cloud_run_service_iam_member.scheduler_invoker,
  ]
}
