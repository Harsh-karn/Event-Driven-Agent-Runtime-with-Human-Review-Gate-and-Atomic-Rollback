# Tether Infrastructure as Code (Terraform)

This directory contains the declarative Terraform modules for deploying **Tether** on Google Cloud Platform (GCP).

## Architecture Components Provisioned

1. **BigQuery Warehouse (`bigquery.tf`)**:
   - `tether_warehouse` dataset.
   - `events` table: Day-partitioned on `ts`, clustered by `agent_id`, `run_id`, and `type`.
   - `v_run_costs` view: Real-time per-run and per-agent cost attribution and token analysis.

2. **Cloud Run v2 Services (`cloud_run.tf`)**:
   - `tether-api`: Public HTTPS endpoint for webhooks, run enqueuing, and human review gate actions (scales to 0).
   - `tether-worker`: Private background worker executing pgmq polling, heartbeat lease renewal, and step replay (scales to 0).

3. **Cloud Scheduler (`cloud_scheduler.tf`)**:
   - `tether-daily-digest-trigger`: Cron job firing scheduled agent runs with authenticated OIDC identity tokens.

4. **Secret Manager (`secrets.tf`)**:
   - `tether-database-url` (Postgres / Supabase connection)
   - `tether-openai-api-key` & `tether-anthropic-api-key` (LiteLLM)
   - `tether-slack-webhook-url` (Human review gate alerts)
   - `tether-webhook-secret` (Inbound HMAC-SHA256 signature verification)

5. **IAM Least Privilege (`iam.tf`)**:
   - Dedicated `tether-worker-sa` (Secret Accessor, BigQuery Data Editor scoped exclusively to `tether_warehouse`).
   - Dedicated `tether-api-sa` (Secret Accessor).
   - Dedicated `tether-scheduler-sa` (Cloud Run Invoker).

6. **Cloud Billing Budget (`budget.tf`)**:
   - Multi-tier budget alert thresholds at 50%, 75%, 90%, and 100% of defined spend limits.

---

## Deployment Instructions

### Prerequisites
- [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk/docs/install)
- [Terraform >= 1.5.0](https://www.terraform.io/downloads)

### 1. Authenticate with GCP
```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 2. Configure Variables
Copy `terraform.tfvars.example` to `terraform.tfvars`:
```bash
cp terraform.tfvars.example terraform.tfvars
```
Edit `terraform.tfvars` with your GCP project ID and region.

### 3. Initialize and Apply
```bash
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

### 4. Populate Secret Values
Once secrets are provisioned, populate their values via `gcloud`:
```bash
echo -n "postgresql://postgres:password@db.supabase.co:5432/postgres" | \
  gcloud secrets versions add tether-database-url --data-file=-

echo -n "sk-ant-..." | \
  gcloud secrets versions add tether-anthropic-api-key --data-file=-

echo -n "https://hooks.slack.com/services/..." | \
  gcloud secrets versions add tether-slack-webhook-url --data-file=-
```

### 5. Tear Down / Destroy
```bash
terraform destroy
```
