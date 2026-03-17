#!/bin/bash
# ============================================================
# AI SRE Agent — Deploy to Google Cloud Run (SECURE)
# Auth proxy = public internet
# Streamlit   = internal only (no direct access)
# ============================================================
set -e

PROJECT_ID="${GCP_PROJECT_ID:-your-project-id}"
REGION="${GCP_DEFAULT_REGION:-us-central1}"
APP_SERVICE="ai-sre-agent"
AUTH_SERVICE="ai-sre-auth"
IMAGE_APP="gcr.io/${PROJECT_ID}/${APP_SERVICE}"
IMAGE_AUTH="gcr.io/${PROJECT_ID}/${AUTH_SERVICE}"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   AI SRE Agent — Secure GCP Deployment          ║"
echo "║   Auth Proxy → public  |  App → internal only   ║"
echo "╚══════════════════════════════════════════════════╝"
echo "Project: ${PROJECT_ID} | Region: ${REGION}"
echo ""

# ── Step 1: Set project ───────────────────────────────────────
echo "▶ Step 1/7 — Setting GCP project..."
gcloud config set project "${PROJECT_ID}"
gcloud config set run/region "${REGION}"

# ── Step 2: Enable APIs ───────────────────────────────────────
echo "▶ Step 2/7 — Enabling GCP APIs..."
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  containerregistry.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  logging.googleapis.com \
  compute.googleapis.com \
  --project="${PROJECT_ID}"
echo "   ✅ APIs enabled"

# ── Step 3: Store secrets ─────────────────────────────────────
echo "▶ Step 3/7 — Storing secrets in Secret Manager..."

# Safe .env parser — handles GOOGLE_CREDENTIALS={"type":...} JSON without
# bash trying to execute it. Exports each key as an env var.
_load_env() {
  if [ ! -f ".env" ]; then return; fi
  python3 - <<'PYEOF'
import re, os

content = open(".env").read()
lines   = content.splitlines()
i = 0
while i < len(lines):
    line = lines[i].strip()
    i += 1
    if not line or line.startswith('#'):
        continue
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
    if not m:
        continue
    key = m.group(1)
    val = m.group(2).strip()
    # Remove surrounding quotes
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1]
    # Handle multiline JSON: keep reading if braces/brackets unbalanced
    depth = val.count('{') - val.count('}') + val.count('[') - val.count(']')
    while depth > 0 and i < len(lines):
        extra = lines[i].strip()
        i += 1
        val += extra
        depth += extra.count('{') - extra.count('}') + extra.count('[') - extra.count(']')
    # Write to temp file
    tmpfile = f"/tmp/.sre_env_{key}"
    with open(tmpfile, "w") as f:
        f.write(val)
PYEOF
}

# Load each secret value from .env via temp files (safe for JSON)
_read_env_var() {
  local KEY=$1
  local TMPFILE="/tmp/.sre_env_${KEY}"
  if [ -f "$TMPFILE" ]; then
    cat "$TMPFILE"
  else
    # Fall back to environment variable if already set
    echo "${!KEY}"
  fi
}

# Parse .env into temp files
_load_env

store_secret() {
  local NAME=$1
  local VALUE=$2
  [ -z "$VALUE" ] && echo "   ⚠️  Skipping ${NAME} (empty)" && return
  if gcloud secrets describe "${NAME}" --project="${PROJECT_ID}" &>/dev/null; then
    echo -n "${VALUE}" | gcloud secrets versions add "${NAME}" \
      --data-file=- --project="${PROJECT_ID}" --quiet
  else
    echo -n "${VALUE}" | gcloud secrets create "${NAME}" --data-file=- \
      --replication-policy="automatic" --project="${PROJECT_ID}"
  fi
  echo "   ✅ ${NAME}"
}

# Read values safely (JSON values won't break bash)
GEMINI_API_KEY_VAL="$(_read_env_var GEMINI_API_KEY)"
GCP_CREDS_VAL="$(_read_env_var GOOGLE_CREDENTIALS)"
TF_BUCKET_VAL="$(_read_env_var TF_STATE_BUCKET)"
AUTH_KEY_VAL="$(_read_env_var AUTH_SECRET_KEY)"
[ -z "$AUTH_KEY_VAL" ] && AUTH_KEY_VAL="$(openssl rand -hex 32)"

store_secret "sre-gemini-api-key"  "${GEMINI_API_KEY_VAL}"
store_secret "sre-gcp-project-id"  "${PROJECT_ID}"
store_secret "sre-gcp-credentials" "${GCP_CREDS_VAL}"
store_secret "sre-tf-state-bucket" "${TF_BUCKET_VAL}"
store_secret "sre-auth-secret-key" "${AUTH_KEY_VAL}"

# ── Step 4: Create service account ───────────────────────────
echo "▶ Step 4/7 — Setting up service account & IAM..."
SA_NAME="${APP_SERVICE}-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="AI SRE Agent SA" --project="${PROJECT_ID}"
fi

for ROLE in \
  "roles/compute.viewer" \
  "roles/storage.admin" \
  "roles/logging.viewer" \
  "roles/monitoring.viewer" \
  "roles/run.viewer" \
  "roles/secretmanager.secretAccessor"
do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" --quiet 2>/dev/null || true
done
echo "   ✅ IAM configured"

# ── Step 5: Build images ──────────────────────────────────────
echo "▶ Step 5/7 — Building Docker images..."

# Build main app
gcloud builds submit \
  --tag "${IMAGE_APP}:latest" \
  --project="${PROJECT_ID}" \
  --timeout=20m .
echo "   ✅ App image: ${IMAGE_APP}:latest"

# Build auth service
gcloud builds submit \
  --tag "${IMAGE_AUTH}:latest" \
  --project="${PROJECT_ID}" \
  --timeout=10m ./auth-service
echo "   ✅ Auth image: ${IMAGE_AUTH}:latest"

# ── Step 6: Deploy Streamlit app (INTERNAL ONLY) ──────────────
echo "▶ Step 6/7 — Deploying Streamlit app (internal only — NOT public)..."
gcloud run deploy "${APP_SERVICE}" \
  --image="${IMAGE_APP}:latest" \
  --platform=managed \
  --region="${REGION}" \
  --port=8080 \
  --memory=2Gi \
  --cpu=2 \
  --min-instances=0 \
  --max-instances=3 \
  --timeout=3600 \
  --service-account="${SA_EMAIL}" \
  --ingress=internal \
  --no-allow-unauthenticated \
  --set-secrets="GEMINI_API_KEY=sre-gemini-api-key:latest,GOOGLE_CREDENTIALS=sre-gcp-credentials:latest,GCP_PROJECT_ID=sre-gcp-project-id:latest,TF_STATE_BUCKET=sre-tf-state-bucket:latest" \
  --set-env-vars="GCP_DEFAULT_REGION=${REGION},GEMINI_MODEL=gemini-3.1-pro-preview,VOICE_MODEL=gemini-2.5-flash,PYTHONUNBUFFERED=1" \
  --project="${PROJECT_ID}"

APP_INTERNAL_URL=$(gcloud run services describe "${APP_SERVICE}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "   ✅ App internal URL: ${APP_INTERNAL_URL}"

# ── Step 7: Deploy Auth Proxy (PUBLIC) ────────────────────────
echo "▶ Step 7/7 — Deploying Auth Proxy (public HTTPS entry point)..."
gcloud run deploy "${AUTH_SERVICE}" \
  --image="${IMAGE_AUTH}:latest" \
  --platform=managed \
  --region="${REGION}" \
  --port=8000 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=10 \
  --timeout=60 \
  --service-account="${SA_EMAIL}" \
  --ingress=all \
  --allow-unauthenticated \
  --set-secrets="AUTH_SECRET_KEY=sre-auth-secret-key:latest" \
  --set-env-vars="STREAMLIT_URL=${APP_INTERNAL_URL},SESSION_TTL_HOURS=8,COOKIE_SECURE=true,AUTH_REALM=AI SRE Agent,LOG_LEVEL=INFO" \
  --project="${PROJECT_ID}"

AUTH_URL=$(gcloud run services describe "${AUTH_SERVICE}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              ✅ DEPLOYMENT COMPLETE!                     ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                          ║"
echo "║  🌐 PUBLIC URL (share this):                             ║"
echo "║  ${AUTH_URL}"
echo "║                                                          ║"
echo "║  🔒 Streamlit (internal only — NOT accessible directly): ║"
echo "║  ${APP_INTERNAL_URL}"
echo "║                                                          ║"
echo "║  👤 Default login:  admin / changeme123                  ║"
echo "║  ⚠️  Change passwords in auth-service/users.json         ║"
echo "║                                                          ║"
echo "║  📸 HACKATHON PROOF LINKS:                               ║"
echo "║  Logs: https://console.cloud.google.com/logs/query       ║"
echo "║        ?project=${PROJECT_ID}                            ║"
echo "║  CR:   https://console.cloud.google.com/run              ║"
echo "║        ?project=${PROJECT_ID}                            ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "For hackathon screen recording:"
echo "1. Visit: https://console.cloud.google.com/run?project=${PROJECT_ID}"
echo "2. Show both services running (ai-sre-agent + ai-sre-auth)"
echo "3. Open ${AUTH_URL} — log in — use the app"
echo "4. Switch to Cloud Logging tab to show live logs"


set -o pipefail   # fail on pipe errors but not on warnings

# ── Embedded .env parser (handles multi-line JSON GOOGLE_CREDENTIALS) ────────
# Writes a temp Python helper and loads the .env safely before anything runs.
_ENVLOADER=$(mktemp /tmp/load_env_XXXX.py)
cat > "$_ENVLOADER" << 'PYEOF'
import sys, re

path  = sys.argv[1] if len(sys.argv) > 1 else ".env"
try:
    raw = open(path).read()
except Exception:
    sys.exit(0)

out   = {}
lines = raw.splitlines()
i     = 0

while i < len(lines):
    stripped = lines[i].strip()
    if not stripped or stripped.startswith("#"):
        i += 1; continue
    if "=" not in stripped:
        i += 1; continue
    key, _, rest = stripped.partition("=")
    key = key.strip()
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
        i += 1; continue
    rest = rest.strip()
    # Multi-line JSON object
    if rest.startswith("{"):
        val   = rest
        depth = val.count("{") - val.count("}")
        while depth > 0 and i + 1 < len(lines):
            i += 1
            val   += "\n" + lines[i]
            depth += lines[i].count("{") - lines[i].count("}")
    # Single-quoted
    elif rest.startswith("'"):
        val = rest[1:]
        while not val.endswith("'") and i + 1 < len(lines):
            i += 1; val += "\n" + lines[i]
        val = val[:-1]
    # Double-quoted
    elif rest.startswith('"'):
        val = rest[1:]
        while not val.endswith('"') and i + 1 < len(lines):
            i += 1; val += "\n" + lines[i]
        val = val[:-1]
    else:
        val = rest

    out[key] = val
    i += 1

for k, v in out.items():
    safe = v.replace("'", "'\\''")
    print(f"export {k}='{safe}'")
PYEOF

if [ -f ".env" ]; then
  eval "$(python3 "$_ENVLOADER" .env)"
  echo "   ✅ .env loaded"
fi
rm -f "$_ENVLOADER"
# ─────────────────────────────────────────────────────────────

# ── EDIT THESE ───────────────────────────────────────────────
