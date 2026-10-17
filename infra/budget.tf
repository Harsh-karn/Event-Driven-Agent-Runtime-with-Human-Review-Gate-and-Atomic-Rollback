# ==============================================================================
# GCP Cloud Billing Budget & Cost Control Alerts
# ==============================================================================

resource "google_billing_budget" "budget_alert" {
  count = var.billing_account_id != "" ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "Tether Monthly Budget Limit (${var.environment})"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = var.budget_amount_usd
    }
  }

  threshold_rules {
    threshold_percent = 0.50 # 50% spend alert
    basis             = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.75 # 75% spend alert
    basis             = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.90 # 90% spend alert
    basis             = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.00 # 100% spend alert (exceeded)
    basis             = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.00 # Forecasted breach
    basis             = "FORECASTED_SPEND"
  }
}
