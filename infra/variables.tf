variable "project_id" {
  description = "Google Cloud Platform Project ID where Tether resources will be provisioned."
  type        = string
}

variable "region" {
  description = "Default Google Cloud region for compute, scheduler, and warehouse resources."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Target deployment environment (e.g. production, staging, development)."
  type        = string
  default     = "production"
}

variable "container_image" {
  description = "Full container image tag in Artifact Registry for Tether API and Worker deployments."
  type        = string
  default     = ""
}

variable "billing_account_id" {
  description = "GCP Billing Account ID used for cost threshold and budget alerting. Leave blank if not configuring budgets."
  type        = string
  default     = ""
}

variable "budget_amount_usd" {
  description = "Monthly spending budget alert limit in USD."
  type        = number
  default     = 50
}

variable "cron_schedule" {
  description = "Cron expression for the Cloud Scheduler recurring trigger (UTC)."
  type        = string
  default     = "0 9 * * *"
}

variable "min_instances_api" {
  description = "Minimum Cloud Run instance count for the Tether API service (0 enables scale-to-zero)."
  type        = number
  default     = 0
}

variable "max_instances_api" {
  description = "Maximum Cloud Run instance count for the Tether API service."
  type        = number
  default     = 10
}

variable "min_instances_worker" {
  description = "Minimum Cloud Run instance count for the Tether Worker service (0 enables scale-to-zero)."
  type        = number
  default     = 0
}

variable "max_instances_worker" {
  description = "Maximum Cloud Run instance count for the Tether Worker service."
  type        = number
  default     = 5
}
