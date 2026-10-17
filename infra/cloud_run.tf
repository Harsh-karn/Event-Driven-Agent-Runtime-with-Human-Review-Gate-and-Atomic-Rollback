# ==============================================================================
# Cloud Run v2 Services: Tether API & Tether Background Queue Worker
# ==============================================================================

locals {
  default_image = "us-central1-docker.pkg.dev/${var.project_id}/tether/runtime:latest"
  app_image     = coalesce(var.container_image, local.default_image)
}

# ------------------------------------------------------------------------------
# 1. Tether API Service (Ingest, Run Status & Review Gate Endpoints)
# ------------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  name     = "tether-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email

    scaling {
      min_instance_count = var.min_instances_api
      max_instance_count = var.max_instances_api
    }

    containers {
      image = local.app_image
      args  = ["uvicorn", "tether.api:app", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      env {
        name  = "APP_ROLE"
        value = "api"
      }

      # Secret Manager References
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.webhook_secret.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "SLACK_WEBHOOK_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.slack_webhook_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  labels = {
    app         = "tether"
    component   = "api"
    environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# 2. Tether Background Worker Service (pgmq Polling, Heartbeat & Engine)
# ------------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "worker" {
  name     = "tether-worker"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.worker.email

    scaling {
      min_instance_count = var.min_instances_worker
      max_instance_count = var.max_instances_worker
    }

    containers {
      image = local.app_image
      args  = ["python", "-m", "tether.worker"]

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      env {
        name  = "APP_ROLE"
        value = "worker"
      }

      env {
        name  = "BIGQUERY_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "BIGQUERY_DATASET"
        value = google_bigquery_dataset.warehouse.dataset_id
      }

      # Secret Manager References
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openai_api_key.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic_api_key.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "SLACK_WEBHOOK_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.slack_webhook_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  labels = {
    app         = "tether"
    component   = "worker"
    environment = var.environment
  }
}
