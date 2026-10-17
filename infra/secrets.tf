# ==============================================================================
# Google Cloud Secret Manager Definitions
# ==============================================================================

resource "google_secret_manager_secret" "database_url" {
  secret_id = "tether-database-url"

  replication {
    auto {}
  }

  labels = {
    app         = "tether"
    environment = var.environment
  }
}

resource "google_secret_manager_secret" "openai_api_key" {
  secret_id = "tether-openai-api-key"

  replication {
    auto {}
  }

  labels = {
    app         = "tether"
    environment = var.environment
  }
}

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "tether-anthropic-api-key"

  replication {
    auto {}
  }

  labels = {
    app         = "tether"
    environment = var.environment
  }
}

resource "google_secret_manager_secret" "slack_webhook_url" {
  secret_id = "tether-slack-webhook-url"

  replication {
    auto {}
  }

  labels = {
    app         = "tether"
    environment = var.environment
  }
}

resource "google_secret_manager_secret" "webhook_secret" {
  secret_id = "tether-webhook-secret"

  replication {
    auto {}
  }

  labels = {
    app         = "tether"
    environment = var.environment
  }
}
