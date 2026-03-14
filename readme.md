# AI SRE Agent — Google Cloud Platform

An AI-powered Site Reliability Engineering platform that manages GCP infrastructure through natural language — chat or voice. Built with Gemini, Terraform, and Streamlit.

---

## What it does

- **Chat & Voice** — describe infrastructure in plain English or speak it, the agent writes and applies the Terraform
- **Deploy Agent** — generates HCL, runs `terraform plan`, shows cost + security audit, applies on confirmation
- **Monitor Agent** — runs every 2 minutes, detects anomalies (VM failures, high error rates, public buckets), alerts via Slack / email / PagerDuty
- **Security Agent** — scans every resource for misconfigurations, auto-fixes issues before apply
- **Observability** — query live GCP state: "list all VMs", "show recent errors", "how big is bucket X"
- **Audit Trail** — every action logged to GCS + local JSONL with full version history and rollback

---

## Architecture

```
User (chat / voice)
        │
   Auth Proxy (JWT)
        │
   Redis Event Bus
   ┌────┴──────────────────────────┐
   │                               │
Deploy Agent          Monitor Agent (background)
   │                       │
Gemini AI ◄────────────────┘
   │
GCP REST APIs → Terraform → GCS Remote State
```

**Services (docker-compose):**

| Container | Role |
|---|---|
| `ai-sre-gcp` | Main Streamlit UI + Deploy/Security/Voice agents |
| `monitor-agent` | Autonomous 24/7 monitoring loop |
| `auth` | Auth proxy with JWT sessions |
| `redis` | Event bus between all services |

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_USERNAME/ai-sre-agent.git
cd ai-sre-agent
cp .env.example .env
```

Edit `.env`:

```env
GCP_PROJECT_ID=your-project-id
GCP_DEFAULT_REGION=us-central1
GEMINI_API_KEY=your-gemini-api-key
GOOGLE_CREDENTIALS={"type":"service_account",...}   # SA JSON as string
TF_STATE_BUCKET=your-gcs-bucket-for-tfstate
```

### 2. GCP service account permissions

The SA needs these roles:

```
roles/compute.admin
roles/storage.admin
roles/logging.viewer
roles/run.admin
roles/iam.securityReviewer
```

### 3. Start

```bash
docker-compose up -d
```

Open `http://localhost:8080`

---

## Example commands

```
create vm prod-api e2-medium us-central1 os=ubuntu-2404-lts
create GKE cluster with 3 nodes in europe-west1
remove all test-* buckets
list all VMs
show recent errors last 30 minutes
what is the disk size of vm prod-api
fix security issues
```

---

## Monitor Agent

Runs as a separate container, polls GCP every 2 minutes.

**Alerts on:**
- VM in unexpected state (not RUNNING)
- Log error rate above threshold (default: 10/min)
- CRITICAL log entries
- Bucket with public access
- Cloud Run service not ready
- Unexpected VM count changes (drift)
- AI-detected anomalies via Gemini

**Alert channels** (configure in `.env`):

```env
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
ALERT_EMAIL_TO=ops@company.com
SMTP_HOST=smtp.gmail.com
SMTP_USER=alerts@company.com
SMTP_PASSWORD=app-password
PAGERDUTY_ROUTING_KEY=your-key   # critical/high alerts only
MONITOR_INTERVAL_SECS=120
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GCP_PROJECT_ID` | ✅ | GCP project ID |
| `GEMINI_API_KEY` | ✅ | Google AI API key |
| `GOOGLE_CREDENTIALS` | ✅ | Service account JSON string |
| `GCP_DEFAULT_REGION` | ✅ | Default region (e.g. `us-central1`) |
| `TF_STATE_BUCKET` | ✅ | GCS bucket for Terraform remote state |
| `GEMINI_MODEL` | — | Override model (default: `gemini-3.1-pro-preview`) |
| `VOICE_MODEL` | — | Voice transcription model (default: `gemini-2.0-flash`) |
| `SLACK_WEBHOOK_URL` | — | Slack alerts |
| `ALERT_EMAIL_TO` | — | Email alert recipients |
| `PAGERDUTY_ROUTING_KEY` | — | PagerDuty critical alerts |
| `MONITOR_INTERVAL_SECS` | — | Monitor poll interval (default: `120`) |
| `CPU_ALERT_PCT` | — | CPU alert threshold (default: `85`) |
| `LOG_ERROR_RATE_THRESHOLD` | — | Errors/min to alert (default: `10`) |

---

## Tech stack

- **AI** — Google Gemini (gemini-3.1-pro-preview, gemini-2.5-flash, gemini-2.0-flash)
- **IaC** — Terraform + GCS remote state
- **UI** — Streamlit
- **Event bus** — Redis
- **Auth** — FastAPI JWT proxy
- **Cloud** — Google Cloud Platform (Compute, Storage, Logging, Run, GKE)
- **Notifications** — Slack, SMTP email, PagerDuty

---

## Project structure

```
.
├── agent.py                  # Core AI agent + all job workers
├── ui.py                     # Streamlit frontend
├── requirements.txt
├── Dockerfile
├── docker-compose.yaml
├── monitor-agent/
│   ├── monitor_agent.py      # Autonomous monitoring service
│   └── Dockerfile
├── auth-service/
│   ├── auth_service.py       # JWT auth proxy
│   └── Dockerfile
└── terraform_files/          # Generated HCL, state, audit log
    ├── main.tf
    ├── provider.tf
    └── audit.jsonl
```

---

## Built for

[Gemini Live Agent Hackathon](https://geminiliveagentchallenge.devpost.com/) — March 2026
