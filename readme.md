# AI SRE Agent — Google Cloud

An AI-powered infrastructure engineer that writes, plans, and applies Terraform on Google Cloud Platform. Talk to it in chat or by voice — it handles the rest.

Built for the [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/).

---

## What it does

- **Generates Terraform HCL** from plain English — "create a VM type micro in us-central1 with Ubuntu 24"
- **Plans before applying** — shows you exactly what will change, with cost estimate and security audit
- **Applies to GCP** — one click deploys real infrastructure
- **Voice commands** — speak your request, review what it heard, send to agent
- **Live GCP queries** — "list all VMs", "show recent logs", "how big is bucket X"
- **Security audit** — detects misconfigurations and auto-fixes them before apply
- **Version history & rollback** — every apply is versioned, one-click revert
- **Remote state** — Terraform state stored in GCS, never lost
- **Audit trail** — every action logged to JSONL locally and in GCS

---

## Requirements

- Docker + Docker Compose
- A GCP project with billing enabled
- A GCP service account JSON key with these roles:
  - `Editor` or `Compute Admin`, `Storage Admin`, `Logging Viewer`
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/)

---

## Quick start

**1. Clone and configure**

```bash
git clone https://github.com/your-username/ai-sre-agent-gcp.git
cd ai-sre-agent-gcp
cp .env.example .env
```

**2. Fill in `.env`**

```env
# Required
GEMINI_API_KEY=your-gemini-api-key
GCP_PROJECT_ID=your-gcp-project-id
GOOGLE_CREDENTIALS={"type":"service_account","project_id":"..."}   # paste SA JSON as one line

# Optional but recommended
GCP_DEFAULT_REGION=us-central1
GEMINI_MODEL=gemini-3.1-pro
VOICE_MODEL=gemini-2.5-flash
TF_STATE_BUCKET=your-gcs-bucket-for-tfstate   # if blank, state is stored locally
```

**3. Start**

```bash
docker-compose up -d
```

Open [http://localhost:8080](http://localhost:8080)

Default login: `admin` / `changeme` — edit `auth-service/users.json` to change.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes | Gemini API key from AI Studio |
| `GCP_PROJECT_ID` | yes | GCP project ID |
| `GOOGLE_CREDENTIALS` | yes* | Service account JSON as a string |
| `GOOGLE_APPLICATION_CREDENTIALS` | yes* | Path to SA JSON file (alternative to above) |
| `GCP_DEFAULT_REGION` | no | Default region (default: `us-central1`) |
| `GEMINI_MODEL` | no | Model for IaC generation (default: `gemini-3.1-pro`) |
| `VOICE_MODEL` | no | Model for voice transcription (default: `gemini-2.5-flash`) |
| `TF_STATE_BUCKET` | no | GCS bucket name for remote Terraform state |
| `TF_STATE_PREFIX` | no | GCS path prefix (default: `ai-sre-agent/terraform.tfstate`) |

*One of `GOOGLE_CREDENTIALS` or `GOOGLE_APPLICATION_CREDENTIALS` is required.

---

## Project structure

```
.
├── agent.py                  # Core agent — Gemini, Terraform, GCP queries
├── ui.py                     # Streamlit UI
├── Dockerfile                # Main app container
├── docker-compose.yaml       # All services
├── .env.example              # Environment template
├── auth-service/
│   ├── auth_service.py       # JWT auth proxy (FastAPI)
│   ├── users.json            # User credentials
│   └── Dockerfile
└── terraform_files/          # Generated HCL, state, audit log (auto-created)
    ├── main.tf
    ├── provider.tf
    ├── terraform.tfstate
    └── audit.jsonl
```

---

## Voice commands

Switch to the **Voice** tab, select a model, record, click **Transcribe**, then **Send to Agent**.

Examples:

```
"create a VM type micro in us-central1 with Ubuntu 24"
"create a GKE cluster with 3 nodes in europe-west1"
"list all my VMs"
"show recent error logs"
"what is the disk size of vm prod-api"
"remove bucket old-logs-bucket"
"what is my billing summary"
```

---

## Example chat commands

```
create a GCS bucket named ml-data in us-central1
create a VM named web-server e2-medium us-central1
create a GKE cluster with 2 nodes in asia-southeast1
remove vm test-vm-01
fix security issues
show me all running VMs
list all storage buckets
show logs from the last 30 minutes
rollback to previous version
```

---

## Security

- All traffic goes through the auth proxy — Streamlit is never exposed directly
- JWT sessions with configurable TTL (default 8 hours)
- Brute-force protection on login
- Every apply is logged in the audit trail with user, timestamp, and resources changed
- `terraform plan` is always shown before any `terraform apply`

---

## Supported GCP resources

Compute VM · GKE cluster · GCS bucket · Cloud Run · Cloud Functions · VPC network · Firewall rules · BigQuery dataset + table · Cloud SQL · Pub/Sub · Load balancer · Cloud Armor

---

## Tech stack

| Layer | Technology |
|---|---|
| AI | Gemini 2.5 Flash / Gemini 3.1 Pro (Google AI) |
| IaC | Terraform (hashicorp/google ~> 5.0) |
| UI | Streamlit |
| Auth | FastAPI + JWT |
| State | GCS remote backend |
| Container | Docker Compose |

---

## Hackathon

Submitted to the **Gemini Live Agent Challenge** — [geminiliveagentchallenge.devpost.com](https://geminiliveagentchallenge.devpost.com/)

Uses: Gemini AI · Google Cloud · Voice input · Terraform IaC · GCS state · Cloud Logging
