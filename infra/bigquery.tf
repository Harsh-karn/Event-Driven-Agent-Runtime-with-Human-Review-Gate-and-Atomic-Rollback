# ==============================================================================
# Tether BigQuery Warehouse & Analytics Engine
# ==============================================================================

resource "google_bigquery_dataset" "warehouse" {
  dataset_id                  = "tether_warehouse"
  friendly_name               = "Tether Run Warehouse"
  description                 = "Durable run log warehouse, outbox events, cost attribution, and timeline replay data."
  location                    = var.region
  default_table_expiration_ms = null # Retain forever for auditability

  labels = {
    app         = "tether"
    environment = var.environment
  }
}

resource "google_bigquery_table" "events" {
  dataset_id = google_bigquery_dataset.warehouse.dataset_id
  table_id   = "events"
  description = "Partitioned event log emitted by Tether runtime; supports replay CLI and cost analytics."

  time_partitioning {
    type  = "DAY"
    field = "ts"
  }

  clustering = ["agent_id", "run_id", "type"]

  schema = jsonencode([
    {
      name        = "id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Unique event UUID"
    },
    {
      name        = "run_id"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Agent execution run UUID"
    },
    {
      name        = "agent_id"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Identifier of the agent executing the step"
    },
    {
      name        = "step_id"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Optional step identifier associated with this event"
    },
    {
      name        = "ts"
      type        = "TIMESTAMP"
      mode        = "REQUIRED"
      description = "Event occurrence timestamp (partition column)"
    },
    {
      name        = "type"
      type        = "STRING"
      mode        = "REQUIRED"
      description = "Event type (e.g. state_transition, model_call, tool_call, approval_decision)"
    },
    {
      name        = "payload"
      type        = "JSON"
      mode        = "NULLABLE"
      description = "Sanitized event payload with PII and secrets redacted"
    },
    {
      name        = "tokens_in"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Input prompt tokens consumed"
    },
    {
      name        = "tokens_out"
      type        = "INTEGER"
      mode        = "NULLABLE"
      description = "Output completion tokens generated"
    },
    {
      name        = "cost_usd"
      type        = "FLOAT"
      mode        = "NULLABLE"
      description = "Calculated cost in USD according to pricing catalog"
    },
    {
      name        = "model"
      type        = "STRING"
      mode        = "NULLABLE"
      description = "Model identifier invoked (e.g. gpt-4o, claude-3-5-sonnet)"
    }
  ])

  labels = {
    app         = "tether"
    environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# Cost Attribution & Replay Views
# ------------------------------------------------------------------------------
resource "google_bigquery_table" "view_run_costs" {
  dataset_id = google_bigquery_dataset.warehouse.dataset_id
  table_id   = "v_run_costs"
  description = "View providing immediate per-run and per-agent cost and token attribution."

  view {
    query          = <<-SQL
      SELECT
        run_id,
        COALESCE(agent_id, 'unknown') AS agent_id,
        MIN(ts) AS run_started_at,
        MAX(ts) AS run_ended_at,
        COUNT(id) AS total_events,
        SUM(COALESCE(tokens_in, 0)) AS total_tokens_in,
        SUM(COALESCE(tokens_out, 0)) AS total_tokens_out,
        ROUND(SUM(COALESCE(cost_usd, 0.0)), 6) AS total_cost_usd
      FROM
        `${var.project_id}.${google_bigquery_dataset.warehouse.dataset_id}.events`
      GROUP BY
        run_id, agent_id
    SQL
    use_legacy_sql = false
  }

  depends_on = [google_bigquery_table.events]
}
