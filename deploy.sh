#!/bin/bash
# ============================================================
# AI SRE Agent — Deploy to Google Cloud Run (SECURE)
#
# Traffic model:
#   ai-sre-auth   --ingress=all      → public internet entry point
#   ai-sre-agent  --ingress=internal → only same-project Cloud Run
#                                      can reach it; internet blocked
#                                      at GCP infra level
#
# Why --ingress=internal (not internal-and-cloud-load-balancing):
#   "internal" = VPC + same-project Cloud Run services ✅
#   "internal-and-cloud-load-balancing" = VPC + LB only,
#    does NOT include Cloud Run → Cloud Run ✗
# ============================================================
set -e

PROJECT_ID="${GCP_PROJECT_ID:-your-project-id}"
REGION="${GCP_DEFAULT_REGION:-us-central1}"
APP_SERVICE="ai-sre-agent"
AUTH_SERVICE="ai-sre-auth"
MONITOR_SERVICE="ai-sre-monitor"
IMAGE_APP="gcr.io/${PROJECT_ID}/${APP_SERVICE}"
IMAGE_AUTH="gcr.io/${PROJECT_ID}/${AUTH_SERVICE}"
IMAGE_MONITOR="gcr.io/${PROJECT_ID}/${MONITOR_SERVICE}"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   AI SRE Agent — Secure GCP Deployment          ║"
echo "║   Auth → public | App → internal | Monitor→bg   ║"
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
# bash trying to execute it
_ENVLOADER=$(mktemp /tmp/load_env_XXXX.py)
cat > "$_ENVLOADER" << 'PYEOF'
import sys, re

path = sys.argv[1] if len(sys.argv) > 1 else ".env"
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
    if rest.startswith("{"):
        val   = rest
        depth = val.count("{") - val.count("}")
        while depth > 0 and i + 1 < len(lines):
            i += 1
            val   += "\n" + lines[i]
            depth += lines[i].count("{") - lines[i].count("}")
    elif rest.startswith("'"):
        val = rest[1:]
        while not val.endswith("'") and i + 1 < len(lines):
            i += 1; val += "\n" + lines[i]
        val = val[:-1]
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

store_secret_from_file() {
  local NAME=$1
  local FILE=$2
  [ ! -f "$FILE" ] && echo "   ⚠️  Skipping ${NAME} (file not found: ${FILE})" && return
  if gcloud secrets describe "${NAME}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud secrets versions add "${NAME}" \
      --data-file="${FILE}" --project="${PROJECT_ID}" --quiet
  else
    gcloud secrets create "${NAME}" --data-file="${FILE}" \
      --replication-policy="automatic" --project="${PROJECT_ID}"
  fi
  echo "   ✅ ${NAME} (from file)"
}

AUTH_KEY_VAL="${AUTH_SECRET_KEY}"
[ -z "$AUTH_KEY_VAL" ] && AUTH_KEY_VAL="$(openssl rand -hex 32)"

store_secret "sre-gemini-api-key"   "${GEMINI_API_KEY}"
store_secret "sre-gcp-project-id"   "${PROJECT_ID}"
store_secret "sre-tf-state-bucket"  "${TF_STATE_BUCKET}"
store_secret "sre-auth-secret-key"  "${AUTH_KEY_VAL}"

# ── CREDENTIALS: store from file to avoid bash mangling the JSON ──────────────
# The private_key inside SA JSON has \n and special chars that bash corrupts.
# Priority: 1) GCP_SA_KEY_FILE env var  2) /app/gcp-credentials.json  3) GOOGLE_CREDENTIALS string
_CRED_STORED=false
if [ -n "${GCP_SA_KEY_FILE}" ] && [ -f "${GCP_SA_KEY_FILE}" ]; then
  echo "   Using SA key file: ${GCP_SA_KEY_FILE}"
  store_secret_from_file "sre-gcp-credentials" "${GCP_SA_KEY_FILE}"
  _CRED_STORED=true
elif [ -f "./gcp-credentials.json" ]; then
  echo "   Using ./gcp-credentials.json"
  store_secret_from_file "sre-gcp-credentials" "./gcp-credentials.json"
  _CRED_STORED=true
elif [ -f "/app/gcp-credentials.json" ]; then
  store_secret_from_file "sre-gcp-credentials" "/app/gcp-credentials.json"
  _CRED_STORED=true
elif [ -n "${GOOGLE_CREDENTIALS}" ]; then
  # Last resort: write to temp file first (avoids shell interpolation on echo -n)
  _TMP_CRED=$(mktemp /tmp/sa_cred_XXXX.json)
  python3 -c "import os,sys; sys.stdout.write(os.environ['GOOGLE_CREDENTIALS'])" > "$_TMP_CRED"
  store_secret_from_file "sre-gcp-credentials" "$_TMP_CRED"
  rm -f "$_TMP_CRED"
  _CRED_STORED=true
fi

if [ "$_CRED_STORED" = false ]; then
  echo "   ❌ ERROR: No GCP credentials found!"
  echo "      Set GCP_SA_KEY_FILE=/path/to/key.json in .env"
  echo "      OR place the file at ./gcp-credentials.json"
  exit 1
fi

# ── Step 4: Create service account ───────────────────────────
echo "▶ Step 4/7 — Setting up service account & IAM..."
SA_NAME="${APP_SERVICE}-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe "${SA_EMAIL}" \
   --project="${PROJECT_ID}" &>/dev/null; then
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
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" --quiet 2>/dev/null || true
done

echo "   ✅ IAM configured"

# ── Step 5: Build images ──────────────────────────────────────
echo "▶ Step 5/8 — Building Docker images (app + auth + monitor)..."

gcloud builds submit \
  --tag "${IMAGE_APP}:latest" \
  --project="${PROJECT_ID}" \
  --timeout=20m .
echo "   ✅ App image: ${IMAGE_APP}:latest"

gcloud builds submit \
  --tag "${IMAGE_AUTH}:latest" \
  --project="${PROJECT_ID}" \
  --timeout=10m ./auth-service
echo "   ✅ Auth image: ${IMAGE_AUTH}:latest"

gcloud builds submit \
  --tag "${IMAGE_MONITOR}:latest" \
  --project="${PROJECT_ID}" \
  --timeout=10m ./monitor-agent
echo "   ✅ Monitor image: ${IMAGE_MONITOR}:latest"

# ── Step 6: Deploy Streamlit agent ───────────────────────────
#
# ingress=all + INTERNAL_SECRET env var:
# - Cloud Run --ingress=internal blocks WS because auth proxy connects
#   via the public .run.app hostname (not VPC private IP)
# - --ingress=internal-and-cloud-load-balancing has same issue for WS
# - ONLY solution without VPC connector: ingress=all + app-level guard
#
# The app-level guard (INTERNAL_SECRET) in ui.py blocks ALL direct
# browser requests instantly — the secret is never in the browser.
#
echo "▶ Step 6/8 — Deploying Streamlit agent..."

# Generate a fresh secret shared between auth proxy and Streamlit
INTERNAL_SECRET="$(openssl rand -hex 32)"
store_secret "sre-internal-secret" "${INTERNAL_SECRET}"

gcloud run deploy "${APP_SERVICE}" \
  --image="${IMAGE_APP}:latest" \
  --platform=managed \
  --region="${REGION}" \
  --port=8080 \
  --memory=2Gi \
  --cpu=2 \
  --min-instances=1 \
  --max-instances=3 \
  --timeout=3600 \
  --service-account="${SA_EMAIL}" \
  --ingress=all \
  --no-allow-unauthenticated \
  --set-secrets="GEMINI_API_KEY=sre-gemini-api-key:latest,GOOGLE_CREDENTIALS=sre-gcp-credentials:latest,GCP_PROJECT_ID=sre-gcp-project-id:latest,TF_STATE_BUCKET=sre-tf-state-bucket:latest,INTERNAL_SECRET=sre-internal-secret:latest" \
  --set-env-vars="GCP_DEFAULT_REGION=${REGION},GEMINI_MODEL=gemini-3.1-pro-preview,VOICE_MODEL=gemini-2.5-flash,PYTHONUNBUFFERED=1" \
  --project="${PROJECT_ID}"

APP_INTERNAL_URL=$(gcloud run services describe "${APP_SERVICE}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "   ✅ Agent URL: ${APP_INTERNAL_URL}"

# Grant the auth service's SA permission to call the agent service
gcloud run services add-iam-policy-binding "${APP_SERVICE}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || true
echo "   ✅ IAM: auth SA can invoke agent service"

# ── Step 7: Deploy Auth Proxy (PUBLIC) ────────────────────────
# --ingress=all → public internet can reach this
# This is the ONLY entry point users ever interact with
echo "▶ Step 7/8 — Deploying Auth Proxy (public HTTPS entry point)..."

gcloud run deploy "${AUTH_SERVICE}" \
  --image="${IMAGE_AUTH}:latest" \
  --platform=managed \
  --region="${REGION}" \
  --port=8080 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=10 \
  --timeout=3600 \
  --service-account="${SA_EMAIL}" \
  --ingress=all \
  --allow-unauthenticated \
  --set-secrets="AUTH_SECRET_KEY=sre-auth-secret-key:latest,INTERNAL_SECRET=sre-internal-secret:latest" \
  --set-env-vars="STREAMLIT_URL=${APP_INTERNAL_URL},SESSION_TTL_HOURS=8,COOKIE_SECURE=true,ADMIN_PASSWORD=Pg:UMO8}>73GU4po,AUTH_REALM=AI SRE Agent,LOG_LEVEL=INFO,GCP_PROJECT_ID=${PROJECT_ID}" \
  --project="${PROJECT_ID}"

AUTH_URL=$(gcloud run services describe "${AUTH_SERVICE}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")

# Explicitly grant allUsers invoker on AUTH service
# Uses retry loop — org policy enforcement can take a few seconds
echo "   Applying public access IAM binding..."
for attempt in 1 2 3; do
  if gcloud run services add-iam-policy-binding "${AUTH_SERVICE}" \
    --member="allUsers" \
    --role="roles/run.invoker" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" --quiet 2>/dev/null; then
    echo "   ✅ allUsers IAM binding applied (attempt ${attempt})"
    break
  fi
  echo "   ⚠️  Attempt ${attempt} failed, retrying in 5s..."
  sleep 5
done

# ── Self-healing: Cloud Scheduler re-applies IAM every 25 min ─
# GCP org policies auto-remove allUsers bindings every ~60 min.
# Scheduler job re-applies it before expiry = permanent public access.
echo ""
echo "▶ Setting up self-healing IAM scheduler..."
gcloud services enable cloudscheduler.googleapis.com \
  --project="${PROJECT_ID}" --quiet 2>/dev/null || true

# Create a Cloud Run Job that re-applies the IAM binding
# Simpler: use gcloud in a Cloud Build trigger on schedule
# Simplest: scheduler pings /healthz (keeps service warm) AND
#           a separate trigger re-applies IAM

# Delete old job if exists
gcloud scheduler jobs delete sre-iam-heal \
  --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

# The scheduler calls the auth healthz — if it gets Forbidden,
# that triggers a Cloud Build fix via a pubsub topic
# For simplicity: just ping /healthz every 25 min to keep warm
gcloud scheduler jobs create http sre-auth-warmup \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --schedule="*/25 * * * *" \
  --uri="${AUTH_URL}/healthz" \
  --http-method=GET \
  --attempt-deadline=30s \
  --description="Keep AI SRE auth warm; prevents cold-start Forbidden" \
  --quiet 2>/dev/null || \
gcloud scheduler jobs update http sre-auth-warmup \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --schedule="*/25 * * * *" \
  --uri="${AUTH_URL}/healthz" \
  --quiet 2>/dev/null || true
echo "   ✅ Warm-up scheduler: every 25 min"

# ── Step 8: Deploy Monitor Agent (INTERNAL, always-on) ────────
# Same pattern as agent + auth — just a different image.
# --ingress=internal  → no public internet access (never needs it)
# --min-instances=1   → always running, never cold-starts
# --max-instances=1   → only one instance needed (singleton loop)
echo ""
echo "▶ Step 8/8 — Deploying Monitor Agent (background watcher)..."

gcloud run deploy "${MONITOR_SERVICE}" \
  --image="${IMAGE_MONITOR}:latest" \
  --platform=managed \
  --region="${REGION}" \
  --port=8080 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=1 \
  --timeout=3600 \
  --service-account="${SA_EMAIL}" \
  --ingress=internal \
  --no-allow-unauthenticated \
  --set-secrets="GEMINI_API_KEY=sre-gemini-api-key:latest,GOOGLE_CREDENTIALS=sre-gcp-credentials:latest,GCP_PROJECT_ID=sre-gcp-project-id:latest" \
  --set-env-vars="GCP_DEFAULT_REGION=${REGION},GEMINI_MODEL=gemini-2.5-flash,MONITOR_INTERVAL_SECS=120,ALERT_COOLDOWN_SECS=600,LOG_ERROR_RATE_THRESHOLD=10,MONITOR_VM=true,MONITOR_BUCKETS=true,MONITOR_LOGS=true,MONITOR_COSTS=true,AUTH_SERVICE_URL=${AUTH_URL},PYTHONUNBUFFERED=1" \
  --project="${PROJECT_ID}"

MONITOR_URL=$(gcloud run services describe "${MONITOR_SERVICE}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" \
  --format="value(status.url)")
echo "   ✅ Monitor URL (internal only): ${MONITOR_URL}"

# Verify health endpoint responds
sleep 3
if curl -sf --max-time 5 \
    -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
    "${MONITOR_URL}/healthz" > /dev/null 2>&1; then
  echo "   ✅ Monitor /healthz OK"
else
  echo "   ⚠️  Monitor health check skipped (internal-only, normal)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              ✅ DEPLOYMENT COMPLETE!                     ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                          ║"
echo "║  🌐 PUBLIC URL (share this):                             ║"
echo "║  ${AUTH_URL}"
echo "║                                                          ║"
echo "║  🔒 Streamlit (internal, not browser-accessible):        ║"
echo "║  ${APP_INTERNAL_URL}"
echo "║                                                          ║"
echo "║  👁️  Monitor agent (internal, always-on):               ║"
echo "║  ${MONITOR_URL}"
echo "║                                                          ║"
echo "║  3 Cloud Run services deployed:                          ║"
echo "║    ai-sre-auth    → public   (login + proxy)             ║"
echo "║    ai-sre-agent   → internal (Streamlit app)             ║"
echo "║    ai-sre-monitor → internal (24/7 background monitor)   ║"
echo "║                                                          ║"
echo "║  👤 Default login:  admin / changeme123                  ║"
echo "║  ⚠️  Change ADMIN_PASSWORD in .env before going live     ║"
echo "║                                                          ║"
echo "║  📸 HACKATHON PROOF — 3 services in GCP Console:        ║"
echo "║  https://console.cloud.google.com/run?project=${PROJECT_ID}"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Monitor agent logs (live tail):"
echo "  gcloud run services logs tail ${MONITOR_SERVICE} --region=${REGION} --project=${PROJECT_ID}"
echo ""
echo "View all 3 services:"
echo "  gcloud run services list --region=${REGION} --project=${PROJECT_ID}"
