# AI SRE Agent — Google Cloud

> **Voice-native AI infrastructure engineer** — speaks Terraform, thinks Gemini, runs on GCP.

Built for the [Gemini Live Agent Challenge](https://geminiliveagentchallenge.devpost.com/) · [GCP Proof](./GCP_API_PROOF.md) · [Deploy to GCP](./deploy_to_gcp.sh)

---

## What is this?

A unified AI platform that replaces your SRE team's most repetitive work. One interface — chat or voice — handles infrastructure creation, live observability queries, security auditing, and 24/7 autonomous monitoring. Everything runs on Google Cloud and is powered by Gemini AI.

**Say:** *"Create a VM type micro in us-central1 with Ubuntu 24"*
**It does:** generates Terraform HCL → runs `terraform plan` → shows cost estimate + security audit → one-click apply to GCP.

**Say:** *"Show me the last API transactions"*
**It does:** queries Cloud Logging REST API → Gemini synthesises the answer → displays in chat.

---

## Key Features

| Feature | Description |
|---|---|
| 🗣️ **Voice commands** | Speak any infrastructure or query request — Gemini transcribes and understands even non-native English |
| 🏗️ **IaC generation** | Writes complete, valid Terraform HCL from natural language |
| 📋 **Plan before apply** | Always shows `terraform plan` diff with cost estimate and security findings |
| 🔒 **Security audit** | Detects misconfigurations and auto-fixes them before apply |
| 👁️ **Live GCP queries** | Ask about VMs, buckets, logs, Cloud Run services, billing in plain English |
| 📡 **Monitor Agent** | Autonomous 24/7 background service — polls GCP APIs, detects anomalies with Gemini AI, fires Slack/email/PagerDuty alerts |
| ⏪ **Version history** | Every apply is versioned — one-click rollback to any previous state |
| 📝 **Audit trail** | Every action logged with user, timestamp, resources — JSONL locally and in GCS |
| 🔐 **Auth proxy** | JWT sessions, brute-force protection, Streamlit never exposed directly |
| ☁️ **Remote state** | Terraform state stored in GCS — never lost, versioned |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Users                                 │
│              Chat · Voice · API · Slack (roadmap)            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                   ┌───────▼────────┐
                   │  Auth Proxy    │  JWT · sessions · rate limiting
                   │  (FastAPI)     │  port 8080
                   └───────┬────────┘
                           │
                   ┌───────▼────────┐
                   │  Streamlit UI  │  Chat + Voice tabs
                   │  (ui.py)       │  Plan cards · Security findings
                   └───────┬────────┘
                           │
              ┌────────────▼─────────────┐
              │       Redis Event Bus    │  pub/sub · job state · alerts
              └──┬──────────┬───────────┘
                 │          │
       ┌─────────▼──┐  ┌────▼──────────┐
       │  Deploy    │  │  Monitor      │
       │  Agent     │  │  Agent        │
       │ (agent.py) │  │ (monitor_     │
       └─────┬──────┘  │  agent.py)    │
             │         └────┬──────────┘
             │              │
    ┌────────▼──────────────▼────────┐
    │         Gemini AI              │
    │  gemini-3.1-pro-preview        │  IaC generation
    │  gemini-2.5-flash              │  Anomaly analysis
    │  gemini-2.0-flash              │  Voice transcription
    └────────────────┬───────────────┘
                     │
    ┌────────────────▼───────────────┐
    │     Google Cloud Platform      │
    │  Compute · Storage · Logging   │
    │  Cloud Run · GKE · Cloud SQL   │
    └────────────────┬───────────────┘
                     │
         ┌───────────┴──────────┐
         ▼                      ▼
   GCS Remote State        Audit JSONL
   (terraform.tfstate)     (audit.jsonl)
```

---

## Project Structure

```
.
├── agent.py                      # Core agent — Gemini AI, Terraform, GCP REST APIs
├── ui.py                         # Streamlit UI — Chat + Voice tabs, plan cards
├── Dockerfile                    # Main app container
├── docker-compose.yaml           # All 4 services
├── requirements.txt
├── deploy_to_gcp.sh              # One-command Cloud Run deployment
├── GCP_API_PROOF.md              # Proof of GCP API usage (hackathon)
│
├── auth-service/
│   ├── auth_service.py           # JWT auth proxy (FastAPI)
│   ├── users.json                # User credentials (edit to add users)
│   └── Dockerfile
│
├── monitor-agent/
│   ├── monitor_agent.py          # Autonomous 24/7 GCP monitoring service
│   └── Dockerfile
│
└── terraform_files/              # Auto-created at runtime
    ├── main.tf                   # Generated infrastructure HCL
    ├── provider.tf               # GCP provider config
    ├── terraform.tfstate         # Local state (synced to GCS)
    └── audit.jsonl               # Append-only audit log
```

---

## Quick Start — Local (Docker)

### Prerequisites

- Docker + Docker Compose
- GCP project with billing enabled
- GCP service account JSON key with roles: `Editor` (or `Compute Admin` + `Storage Admin` + `Logging Viewer`)
- Gemini API key from [Google AI Studio](https://aistudio.google.com/)

### 1. Clone

```bash
git clone https://github.com/your-username/ai-sre-agent-gcp.git
cd ai-sre-agent-gcp
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```env
# ── Required ──────────────────────────────────────────────
GEMINI_API_KEY=your-gemini-api-key-from-aistudio
GCP_PROJECT_ID=your-gcp-project-id
GCP_DEFAULT_REGION=us-central1

# Service account — paste entire JSON as one line
GOOGLE_CREDENTIALS={"type":"service_account","project_id":"...","private_key":"..."}

# ── Recommended ───────────────────────────────────────────
TF_STATE_BUCKET=your-gcs-bucket-name
GEMINI_MODEL=gemini-2.5-flash
VOICE_MODEL=gemini-2.0-flash

# ── Monitor Agent alerts (optional) ──────────────────────
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
ALERT_EMAIL_TO=oncall@yourcompany.com
MONITOR_INTERVAL_SECS=120
```

### 3. Start

```bash
docker-compose up -d
```

Open [http://localhost:8080](http://localhost:8080)

Default login: `admin` / `changeme` — edit `auth-service/users.json` to change.

---

## Cloud Run Deployment

Deploy to GCP with a single command:

```bash
gcloud auth login
export GCP_PROJECT_ID=your-project-id
bash deploy_to_gcp.sh
```

The script automatically:
1. Enables all required GCP APIs
2. Creates a service account with minimal IAM permissions
3. Stores secrets in **Google Secret Manager**
4. Builds Docker image via **Cloud Build**
5. Pushes to **Google Container Registry**
6. Deploys to **Cloud Run** (auto-scaling, 0→3 instances)

```
✅ DEPLOYMENT COMPLETE!
🌐 Live URL: https://ai-sre-agent-abc123-uc.a.run.app
```

---

## Environment Variables Reference

### Core

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | From [AI Studio](https://aistudio.google.com/) |
| `GCP_PROJECT_ID` | ✅ | Your GCP project ID |
| `GOOGLE_CREDENTIALS` | ✅* | Service account JSON as single-line string |
| `GOOGLE_APPLICATION_CREDENTIALS` | ✅* | Alternative: path to SA JSON file |
| `GCP_DEFAULT_REGION` | no | Default region (default: `us-central1`) |
| `GEMINI_MODEL` | no | IaC + query model (default: `gemini-2.5-flash`) |
| `VOICE_MODEL` | no | Voice transcription model (default: `gemini-2.0-flash`) |
| `TF_STATE_BUCKET` | no | GCS bucket for remote state |

*One of `GOOGLE_CREDENTIALS` or `GOOGLE_APPLICATION_CREDENTIALS` required.

### Monitor Agent

| Variable | Default | Description |
|---|---|---|
| `MONITOR_INTERVAL_SECS` | `120` | Poll interval in seconds |
| `ALERT_COOLDOWN_SECS` | `600` | Min seconds between duplicate alerts |
| `CPU_ALERT_PCT` | `85` | CPU % alert threshold |
| `DISK_ALERT_PCT` | `90` | Disk % alert threshold |
| `LOG_ERROR_RATE_THRESHOLD` | `10` | Errors/min alert threshold |
| `SLACK_WEBHOOK_URL` | — | Slack incoming webhook |
| `ALERT_EMAIL_TO` | — | Comma-separated email recipients |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | — | Email config |
| `PAGERDUTY_ROUTING_KEY` | — | PagerDuty Events API v2 key |

---

## Voice Commands

Switch to the **Voice** tab → select a model → record → **🎙️ Transcribe** → review **HEARD** card → **➤ Send to Agent**.

### IaC creation

```
"create a VM type micro in us-central1 with Ubuntu 24"
"create a GKE cluster with 3 nodes in europe-west1"
"create a GCS bucket named ml-data in us-central1"
"remove the bucket named old-logs"
"destroy all test VMs"
```

### Observability

```
"list all my VMs"
"what is the disk size of vm prod-api"
"show me the last API transactions"
"show recent error logs"
"how big is bucket ml-data"
"show all Cloud Run services"
"what is my billing summary"
```

Voice normalises spoken regions (*"us central 1"* → `us-central1`) and OS names (*"ubuntu 24"* → `ubuntu-2404-lts`).

---

## Monitor Agent

Runs as a separate container — autonomous, no human needed.

### What it detects

| Check | Trigger |
|---|---|
| VM stopped/error state | VM status not `RUNNING` |
| High error log rate | Errors/min above threshold |
| Critical log entries | Any `CRITICAL` in Cloud Logging |
| Public bucket risk | Missing uniform IAM access |
| Cloud Run not ready | Service in non-ready state |
| Resource drift | VM count drops unexpectedly |
| Cost spike | Many new resources at once |
| AI anomaly detection | Gemini analyses rolling 10-poll history |

### Alert severity

| Severity | Channels |
|---|---|
| 🔴 Critical | Redis UI + Slack + Email + PagerDuty |
| 🟠 High | Redis UI + Slack + Email + PagerDuty |
| 🟡 Medium | Redis UI + Slack + Email |
| 🔵 Low | Redis UI only |

---

## Security

- Streamlit never exposed directly — all traffic through auth proxy
- JWT sessions with configurable TTL (default 8h)
- Brute-force login protection
- `terraform plan` always shown before any `terraform apply`
- `provider "google"` blocks stripped from model output before writing to `main.tf`
- Snapshot + revert — `main.tf` restored on plan discard
- Full audit trail — user, timestamp, resources on every action
- Secrets in Google Secret Manager (production deployment)

---

## Supported GCP Resources

Compute VM · GKE Cluster · GCS Bucket · Cloud Run · Cloud Functions · VPC Network · Firewall Rules · BigQuery Dataset + Table · Cloud SQL · Pub/Sub · Load Balancer · Cloud Armor

---

## GCP APIs Used

| API | Usage |
|---|---|
| Gemini (`generativelanguage.googleapis.com`) | IaC generation, voice, anomaly analysis |
| Compute Engine | VM list, describe, status |
| Cloud Storage | Remote state, bucket queries, object sizes |
| Cloud Logging | Live log queries, error rate monitoring |
| Cloud Run | Service health queries |
| Secret Manager | Production secrets (Cloud Run deploy) |
| Cloud Build | Docker image builds |
| Container Registry | Docker image storage |

Full proof with code line references: **[GCP_API_PROOF.md](./GCP_API_PROOF.md)**

---

## Tech Stack

| Layer | Technology |
|---|---|
| AI — IaC | Gemini 2.5 Flash / gemini-3.1-pro-preview |
| AI — Voice | Gemini 2.0 Flash (audio understanding) |
| AI — Monitoring | Gemini 2.5 Flash (anomaly detection) |
| IaC engine | Terraform (hashicorp/google ~> 5.0) |
| UI | Streamlit |
| Auth | FastAPI + JWT |
| Event bus | Redis 7 |
| State | GCS remote backend |
| Notifications | Slack · SMTP · PagerDuty |
| Deployment | Docker Compose · Cloud Run |
| Secrets | Google Secret Manager |

---

## Hackathon — Gemini Live Agent Challenge

**[geminiliveagentchallenge.devpost.com](https://geminiliveagentchallenge.devpost.com/) · Prize pool $80,000**

### Criteria satisfied

| Requirement | How |
|---|---|
| Uses Gemini AI | gemini-3.1-pro-preview + gemini-2.5-flash + gemini-2.0-flash |
| Runs on Google Cloud | Cloud Run deployment via `deploy_to_gcp.sh` |
| Uses GCP APIs | Compute, Storage, Logging, Run, Secret Manager, Cloud Build |
| Live agents | Deploy Agent (on-demand) + Monitor Agent (24/7 autonomous) |
| Voice / multimodal | Gemini audio understanding → IaC commands |
| IaC bonus | Full Terraform write + plan + apply pipeline |

### Proof of GCP deployment

- **Live URL:** `https://ai-sre-agent-<hash>-uc.a.run.app` _(update after `bash deploy_to_gcp.sh`)_
- **Code proof:** [GCP_API_PROOF.md](./GCP_API_PROOF.md)
- **Deploy script:** [deploy_to_gcp.sh](./deploy_to_gcp.sh)

---

## Roadmap

- [ ] AWS + Azure multi-cloud support
- [ ] GitHub Actions / GitLab CI integration
- [ ] Slack bot interface
- [ ] Stripe subscription billing (Free / Starter $49 / Growth $199)
- [ ] SOC 2 compliance module
- [ ] Mobile app (React Native)

---

## License

MIT
