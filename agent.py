import os
import re
import json
import base64
import subprocess
import threading
import uuid
import functools
from datetime import datetime

import google.generativeai as genai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TF_DIR = "/app/terraform_files"
os.makedirs(TF_DIR, exist_ok=True)

GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
# NOTE: Update GEMINI_MODEL env var to override if a newer model is released.
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")
# Audio transcription model — must support audio via generate_content (NOT Live API).
# Valid options: gemini-2.5-flash-preview-04-17, gemini-2.0-flash-lite, gemini-2.5-pro-preview-03-25
# Do NOT use gemini-2.5-flash-preview-native-audio-dialog (Live API only, hangs via REST).
VOICE_MODEL        = os.environ.get("VOICE_MODEL", "gemini-2.5-flash")

GCP_PROJECT_ID     = os.environ.get("GCP_PROJECT_ID", "")
GCP_DEFAULT_REGION = os.environ.get("GCP_DEFAULT_REGION", "us-central1")
REDIS_URL          = os.environ.get("REDIS_URL", "")

print = functools.partial(print, flush=True)

# ── Redis (optional — only used if REDIS_URL is set / A7 is running) ─────────
_redis_client = None
def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        return None
    try:
        import redis as _redis
        _redis_client = _redis.from_url(REDIS_URL, decode_responses=True)
        _redis_client.ping()
        print(f"[Redis] Connected: {REDIS_URL}")
        return _redis_client
    except Exception as e:
        print(f"[Redis] Not available: {e}")
        return None

def _redis_publish(channel: str, payload: dict):
    """Publish to Redis — silently no-ops if Redis not available."""
    rc = _get_redis()
    if rc:
        try:
            rc.publish(channel, json.dumps(payload))
        except Exception as e:
            print(f"[Redis] publish error: {e}")

# ---------------------------------------------------------------------------
# GCP Credentials bootstrap
# Supports three auth methods (checked in order):
#   1. GOOGLE_APPLICATION_CREDENTIALS — path to a service account JSON file
#   2. GOOGLE_CREDENTIALS             — raw service account JSON string in env
#   3. Application Default Credentials (gcloud auth) — works on GCE/Cloud Run
# ---------------------------------------------------------------------------
_CRED_FILE = "/app/gcp-credentials.json"

def _bootstrap_credentials():
    """Write GOOGLE_CREDENTIALS JSON string to a file so Terraform can use it."""
    raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not raw:
        # Already using file-based ADC or GOOGLE_APPLICATION_CREDENTIALS
        adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if adc:
            print(f"[auth] Using GOOGLE_APPLICATION_CREDENTIALS={adc}")
        else:
            print("[auth] ⚠️  No explicit credentials found — relying on ADC/metadata server")
        return

    # Validate JSON before writing
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[auth] ❌ GOOGLE_CREDENTIALS is not valid JSON: {e}")
        return

    with open(_CRED_FILE, "w") as f:
        json.dump(parsed, f)

    # Point both Terraform and gcloud SDK at this file
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _CRED_FILE
    print(f"[auth] ✅ Credentials written to {_CRED_FILE} "
          f"(sa={parsed.get('client_email', 'unknown')})")

_bootstrap_credentials()

# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------
JOBS: dict = {}
JOBS_LOCK  = threading.Lock()

def _new_job() -> str:
    jid = str(uuid.uuid4())[:8]
    with JOBS_LOCK:
        JOBS[jid] = {"status": "running", "logs": [], "result": None,
                     "started": datetime.now().isoformat()}
    return jid

def _log(jid, msg):
    print(f"[{jid}] {msg}")
    with JOBS_LOCK:
        JOBS[jid]["logs"].append(msg)

def _finish(jid, result):
    with JOBS_LOCK:
        job = JOBS[jid]
        job["status"] = "done"
        job["result"] = result
        job["ended"]  = datetime.utcnow().isoformat() + "Z"
    # Write audit entry for significant actions
    action   = result.get("status", "done")
    msg      = result.get("message", result.get("output", ""))[:200]
    pd       = result.get("plan_details") or {}
    resources_added    = [r["name"] for r in pd.get("add", [])]
    resources_replaced = [r["name"] for r in pd.get("replace", [])]
    resources_destroyed= [r["name"] for r in pd.get("destroy", [])]
    write_audit_entry({
        "jid":      jid,
        "type":     JOBS[jid].get("_job_type", "unknown"),
        "action":   action,
        "user":     JOBS[jid].get("_user", "system"),
        "message":  msg,
        "resources_added":     resources_added,
        "resources_replaced":  resources_replaced,
        "resources_destroyed": resources_destroyed,
        "auto_fixed":  [f["id"] for f in result.get("auto_fixed", [])],
        "cost_total":  (result.get("cost_estimate") or {}).get("total_monthly"),
        "security_findings": len(result.get("security_audit") or []),
        "started":  JOBS[jid].get("started"),
        "ended":    JOBS[jid].get("ended"),
    })

def _error(jid, msg):
    with JOBS_LOCK:
        JOBS[jid]["status"] = "error"
        JOBS[jid]["result"] = {"status": "error", "message": msg}
        JOBS[jid]["ended"]  = datetime.utcnow().isoformat() + "Z"
    write_audit_entry({
        "jid":    jid,
        "type":   JOBS[jid].get("_job_type", "unknown"),
        "action": "error",
        "user":   JOBS[jid].get("_user", "system"),
        "message": msg[:500],
        "started": JOBS[jid].get("started"),
        "ended":   JOBS[jid].get("ended"),
    })

def get_job(jid):
    with JOBS_LOCK:
        return dict(JOBS.get(jid, {}))

# ---------------------------------------------------------------------------
# Audit Trail
# Writes a structured JSON entry for every agent/apply action.
# Stored in: GCS (if TF_STATE_BUCKET set) → gs://{bucket}/audit/YYYY-MM-DD.jsonl
#            Local fallback: /app/terraform_files/audit.jsonl
# ---------------------------------------------------------------------------
AUDIT_FILE = f"{TF_DIR}/audit.jsonl"

def write_audit_entry(entry: dict):
    """Append a JSON audit log entry — local file + optionally GCS."""
    entry.setdefault("ts", datetime.utcnow().isoformat() + "Z")
    line = json.dumps(entry, separators=(",", ":")) + "\n"

    # Always write locally
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(line)
    except Exception as exc:
        print(f"[audit] local write failed: {exc}")

    # Upload to GCS if bucket is configured
    bucket = os.environ.get("TF_STATE_BUCKET", "")
    if bucket:
        try:
            from google.cloud import storage as gcs
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            blob_name = f"audit/{date_str}.jsonl"
            client = gcs.Client()
            blob   = client.bucket(bucket).blob(blob_name)
            # Append by download + re-upload (GCS has no native append)
            try:
                existing = blob.download_as_text()
            except Exception:
                existing = ""
            blob.upload_from_string(existing + line, content_type="application/x-ndjson")
        except Exception as exc:
            print(f"[audit] GCS write failed: {exc}")


def read_audit_log(max_entries: int = 200) -> list[dict]:
    """Read recent audit entries from local file."""
    if not os.path.exists(AUDIT_FILE):
        return []
    try:
        lines = open(AUDIT_FILE).readlines()
        entries = []
        for l in reversed(lines[-max_entries:]):
            l = l.strip()
            if l:
                try:
                    entries.append(json.loads(l))
                except Exception:
                    pass
        return entries  # newest first
    except Exception:
        return []


def _bootstrap_provider():
    project = GCP_PROJECT_ID
    region  = GCP_DEFAULT_REGION

    prov_path = f"{TF_DIR}/provider.tf"
    # Always regenerate provider.tf so credentials path is up to date
    cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    cred_line = f'  credentials = "{cred_file}"\n' if cred_file else ""
    with open(prov_path, "w") as f:
        f.write(f'''terraform {{
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 5.0"
    }}
  }}
}}

provider "google" {{
  project = "{project}"
  region  = "{region}"
{cred_line}}}
''')
    print(f"[bootstrap] provider.tf written (project={project}, region={region}, creds={bool(cred_file)})")

    # ── backend.tf  (GCS remote state + locking) ────────────────────────────
    state_bucket = os.environ.get("TF_STATE_BUCKET", "")
    state_prefix = os.environ.get("TF_STATE_PREFIX", "ai-sre-agent/terraform.tfstate")
    backend_path = f"{TF_DIR}/backend.tf"

    if state_bucket:
        cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        cred_line = f'  credentials = "{cred_file}"\n' if cred_file else ""
        # Always write backend.tf — ensures credentials + config stay in sync
        with open(backend_path, "w") as f:
            f.write(f'''terraform {{
  backend "gcs" {{
    bucket = "{state_bucket}"
    prefix = "{state_prefix}"
{cred_line}  }}
}}
''')
        print(f"[bootstrap] backend.tf written → gs://{state_bucket}/{state_prefix}")
        # Attempt to create bucket if it doesn't exist yet
        _ensure_state_bucket(state_bucket)
    else:
        # Remove stale backend.tf so Terraform doesn't complain about missing bucket
        if os.path.exists(backend_path):
            os.remove(backend_path)
            print("[bootstrap] Removed stale backend.tf (TF_STATE_BUCKET unset)")
        print("[bootstrap] TF_STATE_BUCKET not set — local state only")


def _gcs_client():
    """Return an authenticated google.cloud.storage.Client."""
    from google.cloud import storage as gcs
    cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if cred_file and os.path.exists(cred_file):
        return gcs.Client.from_service_account_json(cred_file, project=GCP_PROJECT_ID)
    return gcs.Client(project=GCP_PROJECT_ID)


def _ensure_state_bucket(bucket_name: str):
    """Create the GCS state bucket if it doesn't exist. Enables versioning. Pure SDK — no gsutil."""
    region = os.environ.get("TF_STATE_REGION", GCP_DEFAULT_REGION)
    try:
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        if bucket.exists():
            print(f"[state-bucket] Already exists: gs://{bucket_name}")
        else:
            new_bucket = client.create_bucket(
                bucket_name,
                location=region,
                project=GCP_PROJECT_ID,
            )
            # Uniform bucket-level access (no per-object ACLs)
            new_bucket.iam_configuration.uniform_bucket_level_access_enabled = True
            new_bucket.patch()
            print(f"[state-bucket] ✅ Created gs://{bucket_name} in {region}")

        # Enable versioning for state history / cheap rollback
        bucket.reload()
        if not bucket.versioning_enabled:
            bucket.versioning_enabled = True
            bucket.patch()
            print(f"[state-bucket] Versioning enabled on gs://{bucket_name}")
        else:
            print(f"[state-bucket] Versioning already on for gs://{bucket_name}")

        # Label the bucket so it's identifiable
        labels = bucket.labels or {}
        if labels.get("managed-by") != "ai-sre-agent":
            bucket.labels = {**labels, "managed-by": "ai-sre-agent"}
            bucket.patch()

    except Exception as e:
        # Non-fatal — Terraform init will surface a clearer error if bucket truly missing
        print(f"[state-bucket] ⚠️  SDK error (non-fatal): {e}")


_bootstrap_provider()

# ---------------------------------------------------------------------------
# GCP Region management
# ---------------------------------------------------------------------------
GCP_REGIONS = {
    "us-central1":              ["us-central1", "iowa", "us central 1"],
    "us-east1":                 ["us-east1", "south carolina", "us east 1"],
    "us-east4":                 ["us-east4", "northern virginia", "us east 4"],
    "us-east5":                 ["us-east5", "columbus", "us east 5"],
    "us-south1":                ["us-south1", "dallas", "us south 1"],
    "us-west1":                 ["us-west1", "oregon", "us west 1"],
    "us-west2":                 ["us-west2", "los angeles", "la", "us west 2"],
    "us-west3":                 ["us-west3", "salt lake city", "us west 3"],
    "us-west4":                 ["us-west4", "las vegas", "us west 4"],
    "northamerica-northeast1":  ["northamerica-northeast1", "montreal"],
    "northamerica-northeast2":  ["northamerica-northeast2", "toronto"],
    "southamerica-east1":       ["southamerica-east1", "sao paulo"],
    "southamerica-west1":       ["southamerica-west1", "santiago"],
    "europe-west1":             ["europe-west1", "belgium", "eu west 1"],
    "europe-west2":             ["europe-west2", "london", "eu west 2"],
    "europe-west3":             ["europe-west3", "frankfurt", "eu west 3"],
    "europe-west4":             ["europe-west4", "netherlands", "eu west 4"],
    "europe-west6":             ["europe-west6", "zurich", "eu west 6"],
    "europe-west8":             ["europe-west8", "milan", "eu west 8"],
    "europe-west9":             ["europe-west9", "paris", "eu west 9"],
    "europe-north1":            ["europe-north1", "finland", "eu north 1"],
    "europe-central2":          ["europe-central2", "warsaw", "eu central 2"],
    "asia-east1":               ["asia-east1", "taiwan", "asia east 1"],
    "asia-east2":               ["asia-east2", "hong kong", "asia east 2"],
    "asia-northeast1":          ["asia-northeast1", "tokyo", "asia northeast 1"],
    "asia-northeast2":          ["asia-northeast2", "osaka", "asia northeast 2"],
    "asia-northeast3":          ["asia-northeast3", "seoul", "asia northeast 3"],
    "asia-southeast1":          ["asia-southeast1", "singapore", "asia southeast 1"],
    "asia-southeast2":          ["asia-southeast2", "jakarta", "asia southeast 2"],
    "asia-south1":              ["asia-south1", "mumbai", "asia south 1"],
    "asia-south2":              ["asia-south2", "delhi", "asia south 2"],
    "australia-southeast1":     ["australia-southeast1", "sydney"],
    "australia-southeast2":     ["australia-southeast2", "melbourne"],
    "me-west1":                 ["me-west1", "tel aviv"],
    "me-central1":              ["me-central1", "doha"],
    "africa-south1":            ["africa-south1", "johannesburg"],
}

def extract_region_from_prompt(prompt: str) -> str | None:
    """Extract GCP region code from natural language prompt."""
    low = prompt.lower()
    # Direct code match e.g. "in us-central1" or "europe-west2"
    direct = re.search(
        r'\b(us|europe|asia|northamerica|southamerica|australia|me|africa)-[a-z]+\d*(-[a-z]+\d*)?\b',
        low)
    if direct:
        return direct.group(0)
    # Alias match
    for region, aliases in GCP_REGIONS.items():
        for alias in aliases:
            if alias in low:
                return region
    return None

def region_to_alias(region: str) -> str:
    """us-central1 → us_central1"""
    return region.replace("-", "_")

def get_existing_provider_aliases() -> dict:
    """Return {alias: region} from provider.tf."""
    path = f"{TF_DIR}/provider.tf"
    if not os.path.exists(path):
        return {}
    content = open(path).read()
    aliases = {}
    for block in re.finditer(r'provider\s+"google"\s*\{([^}]+)\}', content, re.DOTALL):
        body = block.group(1)
        alias_m  = re.search(r'alias\s*=\s*"([^"]+)"', body)
        region_m = re.search(r'region\s*=\s*"([^"]+)"', body)
        if alias_m and region_m:
            aliases[alias_m.group(1)] = region_m.group(1)
    return aliases

def ensure_provider_alias(region: str, jid: str = "") -> str:
    """Add provider alias to provider.tf if missing. Returns alias name."""
    alias    = region_to_alias(region)
    existing = get_existing_provider_aliases()
    if alias in existing:
        if jid: _log(jid, f"⚡ Provider alias '{alias}' already in provider.tf")
        return alias
    path      = f"{TF_DIR}/provider.tf"
    cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    cred_line = f'  credentials = "{cred_file}"\n' if cred_file else ""
    block = f'''
provider "google" {{
  alias   = "{alias}"
  project = "{GCP_PROJECT_ID}"
  region  = "{region}"
{cred_line}}}
'''
    with open(path, "a") as f:
        f.write(block)
    if jid: _log(jid, f"✅ Added provider alias '{alias}' ({region}) to provider.tf")
    return alias

# ---------------------------------------------------------------------------
# tfstate helpers
# ---------------------------------------------------------------------------
def get_created_resources() -> set:
    """
    Return the set of resource logical names currently in tfstate.
    Uses read_tfstate() which handles both local and GCS remote backend —
    NOT the local terraform.tfstate file which is absent when using remote state.
    """
    try:
        state = read_tfstate()
        return {r["name"] for r in state.get("resources", [])}
    except Exception:
        return set()

def get_pending_resources() -> list:
    code = TerraformTools.read_code()
    if not code:
        return []
    in_code  = re.findall(r'resource\s+"[^"]+"\s+"([^"]+)"', code)
    in_state = get_created_resources()
    return [r for r in in_code if r not in in_state]

# ---------------------------------------------------------------------------
# TerraformTools
# ---------------------------------------------------------------------------
class TerraformTools:

    @staticmethod
    def read_code() -> str:
        try:
            os.makedirs(TF_DIR, exist_ok=True)
            path = f"{TF_DIR}/main.tf"
            return open(path).read() if os.path.exists(path) else ""
        except Exception:
            return ""

    @staticmethod
    def extract_resource_blocks(hcl: str) -> dict:
        """
        Parse HCL and return a dict of all resource blocks:
        { "google_compute_instance.micro_vm": "<full block text>", ... }
        Uses brace-counting — handles nested blocks correctly.
        """
        blocks = {}
        lines  = hcl.splitlines()
        i = 0
        while i < len(lines):
            m = re.match(r'\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', lines[i])
            if m:
                rtype, rname = m.group(1), m.group(2)
                key   = f"{rtype}.{rname}"
                start = i
                depth = 0
                while i < len(lines):
                    for ch in lines[i]:
                        if ch == "{": depth += 1
                        elif ch == "}": depth -= 1
                    i += 1
                    if depth == 0:
                        break
                blocks[key] = "\n".join(lines[start:i])
            else:
                i += 1
        return blocks

    @staticmethod
    def write_code(hcl: str) -> str:
        """
        Append new HCL blocks to main.tf.
        - hcl contains ONLY the new blocks Gemini generated (not the full file)
        - Strips any provider/terraform/required_providers blocks the model accidentally included
        - Checks for duplicate logical names before appending
        - Returns "ok" on success or "DUPLICATE:<name1>,<name2>" if blocks already exist
        """
        path = f"{TF_DIR}/main.tf"

        # ── Strip forbidden top-level blocks ─────────────────────────────
        hcl = TerraformTools._strip_provider_blocks(hcl)
        if not hcl.strip():
            return "ok"  # nothing valid left to write

        existing_hcl    = open(path).read() if os.path.exists(path) else ""
        existing_blocks = TerraformTools.extract_resource_blocks(existing_hcl)
        new_blocks      = TerraformTools.extract_resource_blocks(hcl)

        # Detect duplicate logical names (new block already exists in file)
        duplicates = [key for key in new_blocks if key in existing_blocks]
        if duplicates:
            print(f"[write_code] ⚠️  Duplicate resource blocks: {duplicates}")
            return f"DUPLICATE:{','.join(duplicates)}"

        # Append new blocks to existing file
        separator = "\n\n# ── added ──────────────────────────────────────────────\n"
        new_content = existing_hcl.rstrip() + separator + hcl.strip() + "\n"
        with open(path, "w") as f:
            f.write(new_content)

        return "ok"

    @staticmethod
    def _strip_provider_blocks(hcl: str) -> str:
        """
        Remove provider "google" {}, terraform {}, and required_providers {}
        blocks from HCL text. These belong in provider.tf only.
        Uses brace-depth tracking so nested blocks are handled correctly.
        """
        forbidden_starts = (
            'provider "google"',
            "provider 'google'",
            "terraform {",
            "required_providers {",
        )
        lines  = hcl.splitlines(keepends=True)
        result = []
        depth  = 0
        skip   = False

        for line in lines:
            stripped = line.strip()

            if not skip:
                # Check if this line opens a forbidden block
                if any(stripped.startswith(p) for p in forbidden_starts):
                    skip  = True
                    depth = stripped.count("{") - stripped.count("}")
                    print(f"[write_code] stripped forbidden block: {stripped[:60]}")
                    continue
                result.append(line)
                continue

            # Inside a forbidden block — track depth
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                skip  = False
                depth = 0
            # Skip this line

        return "".join(result)

    @staticmethod
    def snapshot_main_tf() -> str:
        """Save current main.tf to main.tf.pre_plan — called before writing new HCL."""
        src  = f"{TF_DIR}/main.tf"
        dest = f"{TF_DIR}/main.tf.pre_plan"
        try:
            content = open(src).read() if os.path.exists(src) else ""
            with open(dest, "w") as f:
                f.write(content)
            return content
        except Exception as exc:
            print(f"[snapshot] failed: {exc}")
            return ""

    @staticmethod
    def revert_to_snapshot() -> bool:
        """Restore main.tf from main.tf.pre_plan — called on plan expiry/cancel."""
        snap = f"{TF_DIR}/main.tf.pre_plan"
        dest = f"{TF_DIR}/main.tf"
        if not os.path.exists(snap):
            return False
        try:
            content = open(snap).read()
            with open(dest, "w") as f:
                f.write(content)
            os.remove(snap)
            print("[snapshot] main.tf reverted to pre-plan state")
            return True
        except Exception as exc:
            print(f"[snapshot] revert failed: {exc}")
            return False

    @staticmethod
    def clear_snapshot():
        """Delete the snapshot after a successful apply — no longer needed."""
        snap = f"{TF_DIR}/main.tf.pre_plan"
        try:
            if os.path.exists(snap):
                os.remove(snap)
        except Exception:
            pass

    # ── Version History ────────────────────────────────────────────────────────
    VERSIONS_DIR = f"{TF_DIR}/versions"
    MAX_VERSIONS = 20   # keep last 20 successful applies

    @staticmethod
    def save_version(label: str, user: str = "system", action: str = "apply") -> str:
        """
        Save current main.tf + state serial as a versioned snapshot.
        Called after every successful apply or destroy.
        Returns the version_id (timestamp string).
        """
        import time as _t
        os.makedirs(TerraformTools.VERSIONS_DIR, exist_ok=True)

        # Read current main.tf
        tf_path = f"{TF_DIR}/main.tf"
        hcl = open(tf_path).read() if os.path.exists(tf_path) else ""

        # Get current state serial
        state_serial = None
        try:
            state_data = read_tfstate(force=True)
            state_serial = state_data["raw"].get("serial")
        except Exception:
            pass

        # Summarise resources in this version
        resources = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', hcl)

        version_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        entry = {
            "version_id":   version_id,
            "ts":           datetime.utcnow().isoformat() + "Z",
            "label":        label,
            "user":         user,
            "action":       action,
            "state_serial": state_serial,
            "resource_count": len(resources),
            "resources":    [{"type": t, "name": n} for t, n in resources],
            "hcl":          hcl,
        }

        path = os.path.join(TerraformTools.VERSIONS_DIR, f"{version_id}.json")
        with open(path, "w") as f:
            json.dump(entry, f, indent=2)

        # Prune old versions
        try:
            all_versions = sorted(
                [v for v in os.listdir(TerraformTools.VERSIONS_DIR) if v.endswith(".json")])
            for old in all_versions[:-TerraformTools.MAX_VERSIONS]:
                os.remove(os.path.join(TerraformTools.VERSIONS_DIR, old))
        except Exception:
            pass

        print(f"[version] saved {version_id} — {len(resources)} resources, serial={state_serial}")
        return version_id

    @staticmethod
    def list_versions() -> list[dict]:
        """Return version history newest-first (metadata only, no HCL content)."""
        vdir = TerraformTools.VERSIONS_DIR
        if not os.path.exists(vdir):
            return []
        entries = []
        for fname in sorted(os.listdir(vdir), reverse=True):
            if not fname.endswith(".json"):
                continue
            try:
                data = json.load(open(os.path.join(vdir, fname)))
                # Return everything except the full HCL (too large for list)
                entries.append({k: v for k, v in data.items() if k != "hcl"})
            except Exception:
                pass
        return entries

    @staticmethod
    def get_version(version_id: str) -> dict | None:
        """Load full version entry including HCL."""
        path = os.path.join(TerraformTools.VERSIONS_DIR, f"{version_id}.json")
        if not os.path.exists(path):
            return None
        try:
            return json.load(open(path))
        except Exception:
            return None

    @staticmethod
    def restore_version(version_id: str) -> str:
        """
        Restore main.tf from a saved version.
        Does NOT apply — just writes the file and returns the HCL for review.
        Caller should run plan + ask user to confirm before applying.
        """
        entry = TerraformTools.get_version(version_id)
        if not entry:
            return f"❌ Version {version_id} not found."
        hcl = entry.get("hcl", "")
        if not hcl:
            return "❌ Version has no HCL content."
        tf_path = f"{TF_DIR}/main.tf"
        # Snapshot current state before restoring
        TerraformTools.snapshot_main_tf()
        with open(tf_path, "w") as f:
            f.write(hcl)
        print(f"[version] restored {version_id} to main.tf")
        return "ok"


    def remove_resource_blocks(names: list[str] = None,
                               resource_type: str = None) -> tuple[list[str], list[str]]:
        """
        Remove resource blocks from main.tf.

        Args:
          names         — list of logical resource names to remove (e.g. ["test_vm_01", "test_vm_02"])
          resource_type — if set, remove ALL blocks of this type (e.g. "google_compute_instance")
                          special value "__ALL__" removes every resource block

        Returns: (removed_names, not_found_names)
        """
        path = f"{TF_DIR}/main.tf"
        if not os.path.exists(path):
            return [], []

        lines = open(path).readlines()
        result   = []
        removed  = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r'\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', line)
            if m:
                rtype, rname = m.group(1), m.group(2)
                should_remove = (
                    (resource_type == "__ALL__") or
                    (resource_type and rtype == resource_type) or
                    (names and rname in names)
                )
                if should_remove:
                    # Skip the entire block by tracking brace depth
                    depth = 0
                    while i < len(lines):
                        for ch in lines[i]:
                            if ch == '{': depth += 1
                            elif ch == '}': depth -= 1
                        i += 1
                        if depth == 0:
                            break
                    removed.append(rname)
                    continue
            result.append(line)
            i += 1

        with open(path, "w") as f:
            f.writelines(result)

        not_found = [n for n in (names or []) if n not in removed]
        return removed, not_found

    @staticmethod
    def remove_resource_block(resource_name: str) -> str:
        """Legacy single-name wrapper around remove_resource_blocks."""
        removed, _ = TerraformTools.remove_resource_blocks(names=[resource_name])
        if removed:
            return f"✅ '{resource_name}' removed."
        return f"⚠️ '{resource_name}' not found in main.tf."

    @staticmethod
    def _diagnose_init_error(out: str) -> str:
        """Return a human-friendly error message for common terraform init failures."""
        low = out.lower()
        bucket = os.environ.get("TF_STATE_BUCKET", "")
        project = GCP_PROJECT_ID

        # ── 403 / Access Denied ──────────────────────────────────────────────
        if "403" in out or "access denied" in low or "accessdenied" in low:
            sa = "(your service account)"
            cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if cred_file and os.path.exists(cred_file):
                try:
                    import json as _j
                    sa = _j.load(open(cred_file)).get("client_email", sa)
                except Exception:
                    pass
            return (
                f"🔐 GCS Access Denied (HTTP 403)\n\n"
                f"The service account **`{sa}`** does not have permission "
                f"to access the state bucket **`{bucket}`**.\n\n"
                f"**Fix — grant this IAM role in GCP Console:**\n"
                f"```\n"
                f"Project : {project}\n"
                f"Bucket  : {bucket}\n"
                f"Member  : {sa}\n"
                f"Role    : roles/storage.objectAdmin\n"
                f"         (or roles/storage.admin for bucket creation)\n"
                f"```\n"
                f"Go to: https://console.cloud.google.com/storage/browser/{bucket}\n"
                f"→ Permissions tab → Grant Access"
            )

        # ── Bucket does not exist ────────────────────────────────────────────
        if "bucket" in low and ("not exist" in low or "not found" in low or "no such" in low):
            return (
                f"🪣 State bucket **`{bucket}`** does not exist.\n\n"
                f"The agent tried to create it automatically but failed. "
                f"Create it manually:\n"
                f"```\n"
                f"gsutil mb -p {project} -l {GCP_DEFAULT_REGION} gs://{bucket}\n"
                f"```"
            )

        # ── Bad credentials / no credentials ────────────────────────────────
        if "credentials" in low or "application default" in low or "unauthenticated" in low:
            return (
                f"🔑 GCP credentials not found or invalid.\n\n"
                f"Set **GOOGLE_CREDENTIALS** (inline JSON) or "
                f"**GOOGLE_APPLICATION_CREDENTIALS** (path to JSON file) in your `.env`."
            )

        # ── Generic fallback ─────────────────────────────────────────────────
        return f"❌ Init failed:\n{out}"

    @staticmethod
    def run_init(jid: str, force: bool = False) -> str:
        terraform_dir = f"{TF_DIR}/.terraform"
        has_backend   = os.path.exists(f"{TF_DIR}/backend.tf")
        state_bucket  = os.environ.get("TF_STATE_BUCKET", "")

        if not (has_backend or force or not os.path.exists(terraform_dir)):
            _log(jid, "⚡ Already initialized.")
            return "ok"

        def _run(cmd):
            return subprocess.run(cmd, cwd=TF_DIR,
                                  capture_output=True, text=True, timeout=120)

        # Migrate local → GCS on first remote setup
        if state_bucket and os.path.exists(f"{TF_DIR}/terraform.tfstate"):
            _log(jid, "☁️  Migrating local state → GCS...")
            res = _run(["terraform", "init", "-no-color", "-migrate-state", "-force-copy"])
        else:
            _log(jid, "🔧 terraform init...")
            res = _run(["terraform", "init", "-no-color", "-reconfigure"])

        out = res.stdout + res.stderr

        if res.returncode != 0:
            low = out.lower()

            # Hard stop on auth/permission errors — retrying won't help
            if ("403" in out or "access denied" in low or "accessdenied" in low
                    or "credentials" in low or "unauthenticated" in low):
                return TerraformTools._diagnose_init_error(out)

            # Backend config mismatch — retry once with -reconfigure (no migration)
            if "migrate-state" in low or "reconfigure" in low:
                _log(jid, "🔄 Backend mismatch — retrying with -reconfigure...")
                res2 = _run(["terraform", "init", "-no-color", "-reconfigure"])
                out2 = res2.stdout + res2.stderr
                if res2.returncode != 0:
                    return TerraformTools._diagnose_init_error(out2)
                _log(jid, "✅ Initialized (reconfigured).")
            else:
                return TerraformTools._diagnose_init_error(out)

        # After successful remote init, retire local state file
        if state_bucket and os.path.exists(f"{TF_DIR}/terraform.tfstate"):
            os.rename(f"{TF_DIR}/terraform.tfstate",
                      f"{TF_DIR}/terraform.tfstate.migrated")
            _log(jid, "✅ Local state migrated → GCS (local renamed .migrated)")

        _log(jid, "✅ Initialized.")
        global _STATE_CACHE
        _STATE_CACHE = {"ts": 0, "data": None}
        return "ok"

    @staticmethod
    def _check_output_for_errors(out: str) -> str | None:
        """
        Return error string only when apply/destroy clearly did NOT succeed.
        Trust returncode=0 as primary signal; this is a secondary sanity check.
        Only used AFTER a returncode=0 run as a belt-and-suspenders check.
        """
        low = out.lower()
        # Explicit success markers — short-circuit, never treat as failure
        if "apply complete" in low or "destroy complete" in low:
            return None
        # Terraform prints structured errors starting with "╷" or "Error:" on its own line
        import re as _re
        if _re.search(r"^\s*(?:\u2577\s*)?(?:\u2502\s*)?error:", low, _re.MULTILINE):
            return "Terraform error:\n" + out[:400]
        if "reconfigure" in low or "migrate-state" in low:
            return "Backend issue:\n" + out[:400]
        return None

    @staticmethod
    def run_validate(jid: str) -> str:
        # Always init first so provider plugins are present before validate
        init = TerraformTools.run_init(jid)
        if init != "ok":
            return f"❌ Init failed before validate:\n{init}"
        res = subprocess.run(["terraform", "validate", "-no-color"],
                             cwd=TF_DIR, capture_output=True, text=True, timeout=30)
        out = res.stdout + res.stderr
        # "Missing required provider" means providers not downloaded — not an HCL error.
        # run_init should have fixed this; if it still appears, re-init with force.
        if res.returncode != 0 and "missing required provider" in out.lower():
            _log(jid, "🔄 Provider missing after init — forcing re-init...")
            force_init = TerraformTools.run_init(jid, force=True)
            if force_init != "ok":
                return f"❌ Force init failed:\n{force_init}"
            res2 = subprocess.run(["terraform", "validate", "-no-color"],
                                  cwd=TF_DIR, capture_output=True, text=True, timeout=30)
            out = res2.stdout + res2.stderr
            return out if res2.returncode != 0 else "ok"
        return out if res.returncode != 0 else "ok"

    @staticmethod
    def run_plan(jid: str) -> str:
        init = TerraformTools.run_init(jid)
        if init != "ok": return init
        _log(jid, "🔎 terraform validate...")
        val = TerraformTools.run_validate(jid)
        if val != "ok": return f"❌ Validation failed:\n{val}"
        _log(jid, "🔍 terraform plan...")
        res = subprocess.run(["terraform", "plan", "-no-color"],
                             cwd=TF_DIR, capture_output=True, text=True, timeout=120)
        out = res.stdout
        if res.returncode != 0:
            out += f"\n❌ STDERR:\n{res.stderr}"
        _log(jid, "✅ Plan complete.")
        return out

    @staticmethod
    def _extract_error_tail(output: str, lines: int = 40) -> str:
        """Return the last N lines of output — where Terraform prints real errors."""
        all_lines = output.strip().splitlines()
        return "\n".join(all_lines[-lines:]) if len(all_lines) > lines else output

    @staticmethod
    def _parse_409_conflicts(output: str) -> list[dict]:
        """
        Parse terraform apply output for 409 alreadyExists errors.
        Returns list of {"address": "google_compute_network.vm_network",
                         "gcp_id":  "projects/P/global/networks/name"}
        """
        conflicts = []
        lines = output.splitlines()
        # Walk lines looking for 409/alreadyExists errors
        # Then scan forward to find: GCP resource path + terraform address
        i = 0
        while i < len(lines):
            line = lines[i]
            if ("409" in line or "alreadyexists" in line.lower()) and "already exists" in line.lower():
                gcp_id  = None
                address = None
                # GCP path: projects/P/global/..., projects/P/regions/R/..., etc.
                m_path = re.search(r"projects/[a-zA-Z0-9_\-/]+", line)
                if m_path:
                    gcp_id = m_path.group(0).rstrip(".,'\\ ")
                # Scan next ~14 lines (handles │ ╷ ╵ box chars in terraform output)
                for j in range(i + 1, min(i + 14, len(lines))):
                    clean = lines[j].replace("│", "").replace("╷", "").replace("╵", "").strip()
                    m_addr = re.match(r"with\s+([\w.]+),", clean)
                    if m_addr:
                        address = m_addr.group(1)
                        break
                if address:
                    conflicts.append({"address": address, "gcp_id": gcp_id})
            i += 1
        return conflicts

    @staticmethod
    def run_import(jid: str, resource_address: str, gcp_id: str) -> str:
        """Run terraform import to bring an existing GCP resource into state."""
        _log(jid, f"📥 Importing {resource_address} ← {gcp_id}")
        res = subprocess.run(
            ["terraform", "import", "-no-color", resource_address, gcp_id],
            cwd=TF_DIR, capture_output=True, text=True, timeout=120)
        out = res.stdout + res.stderr
        if res.returncode != 0:
            return f"❌ Import failed for {resource_address}:\n{out}"
        _log(jid, f"✅ Imported {resource_address}")
        return "ok"

    @staticmethod
    def run_apply(jid: str) -> str:
        init = TerraformTools.run_init(jid, force=True)
        if init != "ok": return "❌ Init failed before apply:\n" + init
        _log(jid, "🚀 terraform apply...")

        def _do_apply():
            return subprocess.run(
                ["terraform", "apply", "-auto-approve", "-input=false", "-no-color"],
                cwd=TF_DIR, capture_output=True, text=True, timeout=600)

        res      = _do_apply()
        combined = res.stdout + res.stderr

        # ── 409 alreadyExists: auto-import conflicting resources then retry ────
        if res.returncode != 0 and ("409" in combined or "alreadyexists" in combined.lower()):
            conflicts = TerraformTools._parse_409_conflicts(combined)
            if conflicts:
                _log(jid, f"⚠️  {len(conflicts)} resource(s) already exist in GCP — importing...")
                import_errors = []
                for c in conflicts:
                    if not c["gcp_id"]:
                        import_errors.append(f"⚠️  Cannot determine GCP ID for {c['address']}")
                        continue
                    result = TerraformTools.run_import(jid, c["address"], c["gcp_id"])
                    if result != "ok":
                        import_errors.append(result)

                if import_errors:
                    # Some imports failed — surface them clearly
                    tail = "\n".join(import_errors)
                    return ("❌ Apply failed — some resources already exist and could not be imported:\n"
                            + tail + "\n\n~~~ERROR_TAIL~~~\n" + tail)

                # All imports succeeded — retry apply
                _log(jid, "🔄 Retrying apply after imports...")
                res2      = _do_apply()
                combined2 = res2.stdout + res2.stderr
                if res2.returncode != 0:
                    tail = TerraformTools._extract_error_tail(combined2, 50)
                    return "❌ Apply failed after import:\n" + combined2 + "\n\n~~~ERROR_TAIL~~~\n" + tail
                err2 = TerraformTools._check_output_for_errors(combined2)
                if err2:
                    tail = TerraformTools._extract_error_tail(combined2, 50)
                    return "❌ Apply may have failed:\n" + combined2 + "\n\n~~~ERROR_TAIL~~~\n" + tail
                _log(jid, "✅ Apply complete (imported + applied)")
                return combined2
            # 409 but no parseable conflicts — fall through to normal error path

        if res.returncode != 0:
            tail = TerraformTools._extract_error_tail(combined, 50)
            return "❌ Apply failed:\n" + combined + "\n\n~~~ERROR_TAIL~~~\n" + tail
        err = TerraformTools._check_output_for_errors(combined)
        if err:
            tail = TerraformTools._extract_error_tail(combined, 50)
            return "❌ Apply may have failed:\n" + combined + "\n\n~~~ERROR_TAIL~~~\n" + tail
        return combined

    @staticmethod
    def run_apply_targets(jid: str, targets: list[dict]) -> str:
        """
        Apply only specific resources via terraform apply -target=TYPE.NAME ...
        targets: [{"type": "google_compute_instance", "name": "random_name"}, ...]
        """
        init = TerraformTools.run_init(jid, force=True)
        if init != "ok": return "❌ Init failed before targeted apply:\n" + init

        target_flags = [f"-target={t['type']}.{t['name']}" for t in targets]
        names = ", ".join(f"{t['type']}.{t['name']}" for t in targets)
        _log(jid, f"🎯 Targeted apply: {names}")

        def _do_targeted():
            return subprocess.run(
                ["terraform", "apply", "-auto-approve", "-input=false", "-no-color"] + target_flags,
                cwd=TF_DIR, capture_output=True, text=True, timeout=600)

        res      = _do_targeted()
        combined = res.stdout + res.stderr

        # Auto-import on 409 (same as full apply)
        if res.returncode != 0 and ("409" in combined or "alreadyexists" in combined.lower()):
            conflicts = TerraformTools._parse_409_conflicts(combined)
            if conflicts:
                _log(jid, f"⚠️  Importing {len(conflicts)} existing resource(s)...")
                for c in conflicts:
                    if c["gcp_id"]:
                        imp = TerraformTools.run_import(jid, c["address"], c["gcp_id"])
                        if imp != "ok":
                            return "❌ Import failed:\n" + imp
                _log(jid, "🔄 Retrying targeted apply after imports...")
                res2      = _do_targeted()
                combined2 = res2.stdout + res2.stderr
                if res2.returncode != 0:
                    tail = TerraformTools._extract_error_tail(combined2, 50)
                    return "❌ Targeted apply failed after import:\n" + combined2 + "\n\n~~~ERROR_TAIL~~~\n" + tail
                return combined2

        if res.returncode != 0:
            tail = TerraformTools._extract_error_tail(combined, 50)
            return "❌ Targeted apply failed:\n" + combined + "\n\n~~~ERROR_TAIL~~~\n" + tail
        err = TerraformTools._check_output_for_errors(combined)
        if err:
            tail = TerraformTools._extract_error_tail(combined, 50)
            return "❌ Targeted apply may have failed:\n" + combined + "\n\n~~~ERROR_TAIL~~~\n" + tail
        _log(jid, f"✅ Targeted apply complete: {names}")
        return combined

    @staticmethod
    def run_destroy_target(jid: str, res_type: str, res_name: str) -> str:
        _log(jid, f"💣 Destroying {res_type}.{res_name}...")
        init = TerraformTools.run_init(jid, force=True)
        if init != "ok": return f"❌ Init failed before destroy:\n{init}"
        res = subprocess.run(
            ["terraform", "destroy", "-auto-approve", "-input=false",
             "-no-color", f"-target={res_type}.{res_name}"],
            cwd=TF_DIR, capture_output=True, text=True, timeout=300)
        combined = res.stdout + res.stderr
        if res.returncode != 0:
            tail = TerraformTools._extract_error_tail(combined, 50)
            return "❌ Destroy failed (exit " + str(res.returncode) + "):\n" + combined + "\n\n~~~ERROR_TAIL~~~\n" + tail
        err = TerraformTools._check_output_for_errors(combined)
        if err:
            tail = TerraformTools._extract_error_tail(combined, 50)
            return "❌ Destroy may have failed:\n" + combined + "\n\n~~~ERROR_TAIL~~~\n" + tail
        _log(jid, f"✅ Destroy complete: {res_type}.{res_name}")
        return combined

    @staticmethod
    def run_destroy_all(jid: str) -> str:
        _log(jid, "💣 Destroying ALL resources...")
        init = TerraformTools.run_init(jid)
        if init != "ok": return f"❌ Init failed:\n{init}"
        res = subprocess.run(
            ["terraform", "destroy", "-auto-approve", "-input=false", "-no-color"],
            cwd=TF_DIR, capture_output=True, text=True, timeout=600)
        combined = res.stdout + res.stderr
        if res.returncode != 0: return f"❌ Destroy all failed:\n{combined}"
        _log(jid, "✅ All resources destroyed.")
        return combined

# ---------------------------------------------------------------------------
# tfstate reader + resource existence checker (checks BOTH main.tf and tfstate)
# ---------------------------------------------------------------------------
# Cache pulled state so we don't hammer GCS on every poll cycle
_STATE_CACHE: dict = {"ts": 0, "data": None}
_STATE_CACHE_TTL = 15  # seconds


def _parse_state_json(raw: dict) -> dict:
    """Convert raw tfstate JSON into structured resource list."""
    resources = []
    for res in raw.get("resources", []):
        rtype = res.get("type", "")
        rname = res.get("name", "")
        for inst in res.get("instances", []):
            attrs  = inst.get("attributes", {})
            region = (
                attrs.get("region") or
                attrs.get("location") or
                (attrs.get("zone", "") or "").rsplit("-", 1)[0] or
                GCP_DEFAULT_REGION
            )
            # Normalise zone suffix: us-central1-a → us-central1
            if re.match(r".+-[a-z]$", region):
                region = region.rsplit("-", 1)[0]
            resources.append({
                "name":   rname,
                "type":   rtype,
                "region": region,
                "id":     attrs.get("id", ""),
                "attrs":  {k: v for k, v in attrs.items()
                           if k in ("name","location","region","zone","self_link","url")},
            })
    return {"resources": resources, "raw": raw}


def read_tfstate(force: bool = False) -> dict:
    """
    Read tfstate from GCS (via `terraform state pull`) when remote backend is configured,
    otherwise fall back to local terraform.tfstate.
    Results are cached for _STATE_CACHE_TTL seconds to avoid hammering GCS.
    """
    import time
    global _STATE_CACHE
    now = time.time()
    if not force and _STATE_CACHE["data"] and (now - _STATE_CACHE["ts"]) < _STATE_CACHE_TTL:
        return _STATE_CACHE["data"]

    state_bucket = os.environ.get("TF_STATE_BUCKET", "")
    empty = {"resources": [], "raw": {}}

    if state_bucket:
        # ── Remote backend: pull state via Terraform ──────────────────────────
        try:
            res = subprocess.run(
                ["terraform", "state", "pull"],
                cwd=TF_DIR, capture_output=True, text=True, timeout=30)
            if res.returncode != 0 or not res.stdout.strip():
                # State bucket may be empty on first run
                return empty
            raw = json.loads(res.stdout)
            result = _parse_state_json(raw)
        except Exception as e:
            print(f"[state] ⚠️  state pull failed: {e}")
            result = empty
    else:
        # ── Local state file ──────────────────────────────────────────────────
        state_path = f"{TF_DIR}/terraform.tfstate"
        if not os.path.exists(state_path):
            return empty
        try:
            raw    = json.load(open(state_path))
            result = _parse_state_json(raw)
        except Exception:
            return empty

    _STATE_CACHE = {"ts": now, "data": result}
    return result


def get_state_lock_info() -> dict | None:
    """
    Check if a Terraform state lock exists in GCS.
    The hashicorp/google backend writes the lock as: gs://BUCKET/PREFIX.tflock
    Uses google-cloud-storage SDK — no gsutil required.
    Returns {"id", "who", "when", "operation"} if locked, None if free.
    """
    state_bucket = os.environ.get("TF_STATE_BUCKET", "")
    state_prefix = os.environ.get("TF_STATE_PREFIX", "ai-sre-agent/terraform.tfstate")
    if not state_bucket:
        return None

    lock_blob_name = f"{state_prefix}.tflock"
    try:
        client = _gcs_client()
        bucket = client.bucket(state_bucket)
        blob   = bucket.blob(lock_blob_name)
        if not blob.exists():
            return None  # no lock object → unlocked
        lock_raw  = blob.download_as_text(timeout=10)
        lock_data = json.loads(lock_raw)
        return {
            "id":        lock_data.get("ID", "unknown"),
            "who":       lock_data.get("Who", "unknown"),
            "when":      lock_data.get("Created", ""),
            "operation": lock_data.get("Operation", "unknown"),
            "info":      lock_data.get("Info", ""),
        }
    except Exception:
        return None  # any error → assume unlocked (safe default)


def force_unlock_state(lock_id: str) -> str:
    """Force-unlock a stuck Terraform state lock."""
    res = subprocess.run(
        ["terraform", "force-unlock", "-force", lock_id],
        cwd=TF_DIR, capture_output=True, text=True, timeout=30)
    return res.stdout + res.stderr


def get_tfstate_summary_text() -> str:
    """Return a human-readable summary of tfstate for injection into Gemini context."""
    state = read_tfstate()
    if not state["resources"]:
        return "tfstate: empty (no resources applied yet)"
    lines = ["tfstate resources (already applied in GCP):"]
    for r in state["resources"]:
        attrs_str = ", ".join(f"{k}={v}" for k, v in r["attrs"].items() if v)
        lines.append(
            f"  - [{r['type']}] name={r['name']} region={r['region']}"
            + (f" | {attrs_str}" if attrs_str else "")
        )
    return "\n".join(lines)


def _list_all_resource_names() -> list:
    """Return all logical resource names from BOTH main.tf and tfstate."""
    names = set()
    # From main.tf
    hcl = TerraformTools.read_code()
    if hcl:
        names.update(re.findall(r'resource\s+"[^"]+"\s+"([^"]+)"', hcl))
    # From tfstate
    for r in read_tfstate()["resources"]:
        names.add(r["name"])
    return sorted(names)


def get_resource_info(res_name: str) -> dict | None:
    """
    Look up a resource by logical name in main.tf first, then tfstate.
    Returns {"name", "type", "region", "source": "hcl"|"state"} or None.
    """
    # ── 1. Search main.tf ─────────────────────────────────────────────────────
    hcl = TerraformTools.read_code()
    if hcl:
        alias_to_region = get_existing_provider_aliases()
        for block in re.finditer(
            r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
            hcl, re.DOTALL
        ):
            rtype, rname, body = block.group(1), block.group(2), block.group(3)
            if rname == res_name:
                alias_m = re.search(r'provider\s*=\s*google\.(\S+)', body)
                if alias_m:
                    alias  = alias_m.group(1).strip()
                    region = alias_to_region.get(alias, alias.replace("_", "-"))
                else:
                    region = GCP_DEFAULT_REGION
                return {"name": rname, "type": rtype, "region": region, "source": "hcl"}

    # ── 2. Search tfstate ─────────────────────────────────────────────────────
    for r in read_tfstate()["resources"]:
        if r["name"] == res_name:
            return {"name": r["name"], "type": r["type"],
                    "region": r["region"], "source": "state"}

    return None


# Keep old name as alias for backwards compat
def get_resource_info_from_hcl(res_name: str) -> dict | None:
    return get_resource_info(res_name)


# ---------------------------------------------------------------------------
# Plan parser — extract per-resource add/change/destroy + region info
# ---------------------------------------------------------------------------
def parse_plan_details(plan_text: str, hcl: str) -> dict:
    """
    Parse terraform plan output and enrich with region info from main.tf.
    Returns:
      {
        "summary": "3 to add, 0 to change, 1 to destroy",
        "add":     [{"name":"vm-1","type":"google_compute_instance","region":"us-central1"}],
        "change":  [...],
        "destroy": [...],
        "replace": [...],
      }
    """
    import re as _re

    # ── 1. Per-resource region map from main.tf ──────────────────────────────
    alias_to_region = get_existing_provider_aliases()  # {alias: region}
    # Build {resource_name: region} from HCL
    res_region: dict = {}
    if hcl:
        for block in _re.finditer(
            r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}',
            hcl, _re.DOTALL
        ):
            rtype, rname, body = block.group(1), block.group(2), block.group(3)
            alias_m = _re.search(r'provider\s*=\s*google\.(\S+)', body)
            if alias_m:
                alias  = alias_m.group(1).strip()
                region = alias_to_region.get(alias, alias.replace("_", "-"))
            else:
                region = GCP_DEFAULT_REGION
            res_region[rname] = region

    # ── 2. Parse plan output for action lines ────────────────────────────────
    add, change, destroy, replace = [], [], [], []

    for line in plan_text.splitlines():
        line = line.strip()
        # Terraform plan action lines look like:
        #   # google_compute_instance.micro-vm will be created
        #   # google_storage_bucket.my-bucket will be destroyed
        #   # google_compute_instance.vm-1 must be replaced
        #   # google_compute_instance.vm-1 will be updated in-place
        m = _re.match(r'#\s+([\w.]+)\.([\w-]+)\s+(.+)', line)
        if not m:
            continue
        rtype, rname, verb = m.group(1), m.group(2), m.group(3).strip()
        region = res_region.get(rname, GCP_DEFAULT_REGION)
        entry  = {"name": rname, "type": rtype, "region": region}

        if "will be created" in verb or "will be read" in verb:
            add.append(entry)
        elif "will be destroyed" in verb:
            destroy.append(entry)
        elif "must be replaced" in verb:
            replace.append(entry)
        elif "will be updated" in verb:
            change.append(entry)

    # ── 3. Summary line ───────────────────────────────────────────────────────
    summary = ""
    for line in plan_text.splitlines():
        if "to add" in line and "to change" in line:
            summary = line.strip()
            break

    return {
        "summary": summary,
        "add":     add,
        "change":  change,
        "destroy": destroy,
        "replace": replace,
    }


# ---------------------------------------------------------------------------
# AI-Powered Cost Estimation & Security Audit — Gemini calls
# ---------------------------------------------------------------------------
# Both functions call Gemini directly with the HCL + plan context.
# No static lookup tables, no hardcoded rules — Gemini reasons over the
# actual resource configuration and returns structured JSON.
# ---------------------------------------------------------------------------

_AI_ANALYSIS_CACHE: dict = {}   # jid → {cost, security} — cleared per job


def _call_gemini_analysis(prompt: str, system: str, jid: str, label: str) -> dict:
    """
    Shared helper: call Gemini with a structured-output prompt.
    Returns parsed JSON dict, or {} on failure.
    """
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model_client = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system,
            generation_config=genai.GenerationConfig(
                max_output_tokens=4096,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        _log(jid, f"🤖 {label} — calling Gemini...")
        response = model_client.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r'```json|```', '', raw).strip()
        result = json.loads(raw)
        _log(jid, f"✅ {label} complete")
        return result
    except Exception as exc:
        _log(jid, f"⚠️  {label} failed: {exc}")
        return {}


# ── Cost Estimation ───────────────────────────────────────────────────────────

_COST_SYSTEM = """You are a GCP billing expert. Analyse the Terraform HCL and plan details provided.
Return ONLY a JSON object — no markdown, no explanation outside the JSON.

JSON schema:
{
  "items": [
    {
      "name": "<terraform resource name>",
      "type": "<google_resource_type>",
      "region": "<gcp region>",
      "size": "<machine type / tier / null>",
      "monthly_usd": <float or null>,
      "note": "<short note: e.g. \"e2-micro us-central1\" or \"free tier\">"
    }
  ],
  "total_monthly": <float>,
  "currency": "USD",
  "disclaimer": "Estimates based on GCP public list prices. Actual charges depend on usage.",
  "pricing_notes": "<brief explanation of key cost drivers>"
}

Rules:
- Only include resources that are being ADDED or REPLACED (not existing unchanged ones)
- Use current GCP public list prices (us-central1 baseline, apply regional multipliers)
- For VMs: base the estimate on machine type × 730 hours/month
- For GKE: control plane ($0.10/hr) + node pool VMs
- For Cloud SQL: instance tier cost
- For GCS: estimate ~100GB unless size is specified; $0.02/GB/month standard storage
- For free resources (VPCs, firewall rules, subnets): monthly_usd = 0, note = "free"
- For unknown resource types: monthly_usd = null, note = "pricing unavailable"
- Regional multipliers (relative to us-central1=1.0): europe-west2=1.18, asia-east2=1.24, etc.
"""


def estimate_plan_cost(plan_details: dict, hcl: str, jid: str = "") -> dict:
    """
    Use Gemini to estimate monthly GCP cost for resources in the plan.
    Returns {"items": [...], "total_monthly": float, "currency": "USD", ...}
    """
    resources_to_price = (
        plan_details.get("add", []) + plan_details.get("replace", [])
    )
    if not resources_to_price or not hcl:
        return {"items": [], "total_monthly": 0.0, "currency": "USD",
                "disclaimer": "", "pricing_notes": ""}

    prompt = f"""Estimate the monthly GCP cost for these resources being created/replaced.

Resources being added/replaced:
{json.dumps(resources_to_price, indent=2)}

Terraform HCL (main.tf):
```hcl
{hcl}
```

Return the cost estimate JSON as specified."""

    result = _call_gemini_analysis(prompt, _COST_SYSTEM, jid, "💰 Cost estimation")
    if not result or "items" not in result:
        return {"items": [], "total_monthly": 0.0, "currency": "USD",
                "disclaimer": "Cost estimation unavailable.", "pricing_notes": ""}
    return result


# ── Security Audit ────────────────────────────────────────────────────────────

_SECURITY_SYSTEM = """You are a senior GCP security engineer and Terraform expert.
Analyse the Terraform HCL provided and identify security issues.
Return ONLY a JSON object — no markdown, no explanation outside the JSON.

JSON schema:
{
  "findings": [
    {
      "id": "<category code e.g. VM-001, FW-001, GCS-001, GKE-001, SQL-001>",
      "severity": "HIGH" | "MEDIUM" | "LOW",
      "title": "<short title>",
      "resource_name": "<terraform resource logical name>",
      "resource_type": "<google_resource_type>",
      "detail": "<1-2 sentence explanation of the risk>",
      "fix": "<exact HCL attribute or block to add/change, as a code snippet>",
      "patchable": true | false
    }
  ],
  "summary": "<1 sentence overall posture summary>"
}

Check for (but not limited to):
VM / Compute:
  - No dedicated service account (using default Compute Engine SA)
  - Public IP via access_config
  - Using default VPC network
  - Shielded VM not enabled
  - OS login not configured

Firewall rules:
  - SSH (22) or RDP (3389) open to 0.0.0.0/0
  - All protocols/ports open (allow all)
  - Overly broad source ranges

GCS Buckets:
  - uniform_bucket_level_access not set to true
  - Versioning disabled
  - No lifecycle rules
  - Public access not explicitly prevented

Cloud SQL:
  - ipv4_enabled = true (public IP)
  - No automated backup_configuration
  - No deletion_protection
  - SSL not enforced

GKE:
  - master_authorized_networks_config missing
  - private_cluster_config missing
  - legacy ABAC enabled
  - Shielded nodes not enabled
  - Network policy not enabled

General:
  - Resources missing labels/tags for cost attribution
  - Deletion protection not set on stateful resources
  - Encryption key not specified (using Google-managed default)

Rules:
- Only audit resources listed in the plan (being ADDED or REPLACED)
- patchable=true means a single HCL attribute addition/change fixes it
- patchable=false means structural changes (new resources, architectural decisions) are needed
- Sort findings: HIGH first, then MEDIUM, then LOW
- Be specific: include the exact resource name and exact HCL fix
"""


def audit_security(hcl: str, plan_details: dict, jid: str = "") -> list[dict]:
    """
    Use Gemini to perform a security audit of the HCL being deployed.
    Returns list of findings: [{id, severity, title, resource_name, detail, fix, patchable}]
    """
    resources_in_plan = (
        plan_details.get("add", []) + plan_details.get("replace", [])
    )
    if not resources_in_plan or not hcl:
        return []

    prompt = f"""Perform a security audit on this Terraform HCL.
Only audit the resources being ADDED or REPLACED (listed below).

Resources being added/replaced:
{json.dumps(resources_in_plan, indent=2)}

Terraform HCL (main.tf):
```hcl
{hcl}
```

Return the security findings JSON as specified."""

    result = _call_gemini_analysis(prompt, _SECURITY_SYSTEM, jid, "🔒 Security audit")
    findings = result.get("findings", []) if result else []

    # Normalise: ensure patchable field exists
    for f in findings:
        f.setdefault("patchable", False)
    return findings


# ── Auto-Fix Security (Gemini rewrites the HCL) ───────────────────────────────

_FIX_SYSTEM = """You are a senior GCP security engineer and Terraform expert.
You will be given Terraform HCL and a list of security findings to fix.
Rewrite the HCL to resolve ALL the listed findings.

Rules:
- PRESERVE every existing resource block — do not remove or rename anything
- Only ADD or MODIFY the specific attributes needed to fix each finding
- Output ONLY the complete corrected main.tf HCL — no JSON wrapper, no markdown fences
- The output must be 100% valid Terraform HCL
- For each finding you fix, add an inline comment: # fixed: <finding-id>
"""


def auto_fix_security(hcl: str, plan_details: dict, jid: str = "",
                      fix_ids: list[str] | None = None) -> tuple[str, list[dict]]:
    """
    Use Gemini to rewrite HCL fixing the specified security findings (or all patchable ones).
    Returns (fixed_hcl, list_of_fixes_applied).
    """
    findings = audit_security(hcl, plan_details, jid)
    if not findings:
        return hcl, []

    # Filter to patchable findings, optionally to specific IDs
    to_fix = [f for f in findings if f.get("patchable", False)]
    if fix_ids and "ALL" not in fix_ids:
        to_fix = [f for f in to_fix if f["id"] in fix_ids]

    if not to_fix:
        return hcl, []

    findings_text = json.dumps([
        {"id": f["id"], "resource_name": f["resource_name"],
         "resource_type": f["resource_type"], "fix": f["fix"]}
        for f in to_fix
    ], indent=2)

    prompt = f"""Fix these security findings in the Terraform HCL below.

Findings to fix:
{findings_text}

Current main.tf:
```hcl
{hcl}
```

Return ONLY the complete corrected HCL — no markdown, no JSON, just the raw HCL."""

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model_client = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=_FIX_SYSTEM,
            generation_config=genai.GenerationConfig(
                max_output_tokens=8192,
                temperature=0.1,
                # Plain text output — not JSON mode
            ),
        )
        _log(jid, f"🔒 Auto-fixing {len(to_fix)} finding(s) via Gemini...")
        response   = model_client.generate_content(prompt)
        fixed_hcl  = re.sub(r'```(?:hcl|terraform)?', '', response.text).strip().strip('`')

        if not fixed_hcl or len(fixed_hcl) < 20:
            _log(jid, "⚠️  Auto-fix returned empty HCL — skipping")
            return hcl, []

        _log(jid, f"✅ Auto-fix complete — {len(to_fix)} finding(s) patched")
        return fixed_hcl, to_fix

    except Exception as exc:
        _log(jid, f"⚠️  Auto-fix failed: {exc}")
        return hcl, []


# Expose _SECURITY_RULES as an empty list for backward-compat with ui.py import
_SECURITY_RULES: list = []


# ---------------------------------------------------------------------------
# Gemini API — HCL generator with auto-retry on validation errors
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an expert Terraform/GCP infrastructure engineer.

Your job: generate COMPLETE, VALID Terraform HCL for any Google Cloud resource the user requests.

## CRITICAL RULES
- NEVER generate terraform {} block
- NEVER generate required_providers
- NEVER generate provider "google" blocks — they are pre-configured in provider.tf
- NEVER generate terraform { required_providers { ... } } blocks
- main.tf must contain ONLY: resource, data, locals, output blocks
- Any provider or terraform block you output will be SILENTLY STRIPPED — do not include them

## OUTPUT FORMAT
Always respond with a JSON object:
{
  "action": "write" | "destroy" | "apply_target" | "fix_security" | "check_drift" | "query" | "deploy" | "info",
  "hcl": "<NEW blocks only — NOT the full file. Empty string for destroy/fix_security/info/query/deploy>",
  "resource_name": "<single logical name — only for single-resource context>",
  "resource_type": "<google_resource_type>",
  "destroy_targets": [{"type": "google_compute_instance", "name": "vm_name"}],
  "apply_targets": [{"type": "google_compute_instance", "name": "random_name"}],
  "fix_ids": ["VM-001", "FW-001"],
  "query": {
    "type": "vm_info"|"vm_metrics"|"bucket_info"|"bucket_size"|"logs"|"list_vms"|"list_buckets"|"run_services"|"billing"|"generic",
    "resource_name": "<name of the resource to query, if specific>",
    "resource_type": "<google resource type if known>",
    "metric": "<cpu|memory|disk|network — for vm_metrics>",
    "minutes": 60,
    "filter": "<optional log filter string>"
  },
  "deploy": {
    "repo_url":    "<https://github.com/owner/repo or other git URL>",
    "branch":      "<branch name, default main>",
    "app_name":    "<name for the deployed service>",
    "environment": "<production|staging|dev>",
    "prompt":      "<user's original deployment intent in plain English>"
  },
  "message": "<explanation to user>"
}

## RULES FOR deploy action
Use action="deploy" when the user wants to deploy an application from a git repository:
- "deploy my app from https://github.com/..." → deploy action
- "deploy the repo https://..." → deploy action
- "deploy app XYZ to production" with a URL → deploy action
Extract: repo_url (required), branch (default main), app_name (from repo name or user), environment, prompt.
Set hcl="" — the deploy engine reads the repo and generates HCL itself.

## RULES FOR check_drift action
Use action="check_drift" when the user asks about drift, infrastructure changes, or state vs reality:
- "check infrastructure drift"
- "check drift" / "detect drift" / "show drift"
- "has anything changed outside terraform"
- "is my infrastructure in sync"
- "what changed in GCP console"
- "terraform state vs reality"
Set hcl="" and message="Checking infrastructure drift..." for this action.
Do NOT use query action for drift — check_drift runs terraform plan -refresh-only which is the correct tool.

## RULES FOR query action
Use action="query" when the user asks about the STATE or METRICS of existing GCP resources:
- "what is the CPU of vm X" → query.type=vm_metrics, metric=cpu
- "what's the disk size / storage of vm X" → query.type=vm_info
- "show me last API transactions / logs" → query.type=logs
- "list all VMs / instances" → query.type=list_vms
- "list all buckets" → query.type=list_buckets
- "how big is bucket X" → query.type=bucket_size
- "show Cloud Run services" → query.type=run_services
- "what's the cost / billing this month" → query.type=billing
- any other GCP state question → query.type=generic
Do NOT generate HCL for query actions. Set hcl="" and fill the query object.

## RULES FOR write action
### OUTPUT ONLY THE NEW RESOURCE BLOCKS — NEVER THE FULL FILE
- The current main.tf is provided in context — it already exists on disk. DO NOT repeat it.
- Your "hcl" field must contain ONLY the new resource block(s) the user is asking to add.
- Do NOT include any existing resource blocks from the current main.tf in your output.
- The engine will APPEND your new blocks to the existing main.tf automatically.
- If the user asks to add a new VM alongside an existing VM, output ONLY the new VM block.
### NAMING NEW RESOURCES
- Always use the EXACT name the user provides if they explicitly give one.
- If the user did NOT give an explicit resource name (e.g. said "create a vm type micro" with no name), AUTO-GENERATE a unique name: combine type abbreviation + region suffix, e.g. `vm-micro-usc1`, `vm-medium-euw1`, `bucket-prod-usc1`.
- Machine type words (micro, small, medium, standard) are NOT resource names — they indicate the e2-micro / e2-small / e2-medium / e2-standard-2 machine type.
- If a resource with the generated or provided logical name already exists in current main.tf: return action="info" telling the user it already exists and suggesting a different name.
### OTHER RULES
- Provider aliases ARE ALREADY in provider.tf — REFERENCE them as: provider = google.ALIAS
- Alias naming: replace hyphens with underscores  (europe-west2 → europe_west2)
- ALWAYS add provider = google.ALIAS to every resource + data source that uses a non-default region
- Resource type reference:
    GCS bucket         → google_storage_bucket (NO acl arg, use IAM; NO region inside resource)
    Compute VM         → google_compute_instance + google_compute_network + google_compute_subnetwork
    Cloud SQL          → google_sql_database_instance + google_sql_database + google_sql_user
    GKE cluster        → google_container_cluster + google_container_node_pool
    Cloud Run          → google_cloud_run_v2_service + google_cloud_run_v2_service_iam_member
    Pub/Sub            → google_pubsub_topic + google_pubsub_subscription
    Cloud Functions    → google_cloudfunctions2_function
    Firewall rule      → google_compute_firewall
    Load Balancer      → google_compute_backend_service + google_compute_url_map
                         + google_compute_global_forwarding_rule
    VPC                → google_compute_network + google_compute_subnetwork
    BigQuery           → google_bigquery_dataset + google_bigquery_table
    Cloud Armor        → google_compute_security_policy
- VM image families: "debian-cloud/debian-12" or "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
- Machine types: e2-micro, e2-small, e2-medium, n2-standard-2, etc.
- OS kwargs from voice commands (os=<value> in user message):
    os=ubuntu-2404-lts  -> project="ubuntu-os-cloud"  family="ubuntu-2404-lts-amd64"
    os=ubuntu-2204-lts  -> project="ubuntu-os-cloud"  family="ubuntu-2204-lts-amd64"
    os=ubuntu-2004-lts  -> project="ubuntu-os-cloud"  family="ubuntu-2004-lts-amd64"
    os=debian-12        -> project="debian-cloud"      family="debian-12"
    os=rocky-linux-9    -> project="rocky-linux-cloud" family="rocky-linux-9"
    os=windows-2022     -> project="windows-cloud"     family="windows-2022"
    os=cos-stable       -> project="cos-cloud"         family="cos-stable"
  Use data "google_compute_image" to resolve: data.google_compute_image.<name>.self_link
- disk= kwarg: disk=50 means boot disk size_gb = 50
- Resource names: lowercase + hyphens only (no underscores in Terraform logical names)
- Do NOT hardcode project IDs inside resource blocks — rely on the provider default

## RULES FOR destroy action
- Supports destroying ONE resource, MANY named resources, or ALL resources of a type
- Output format for destroy:
  {
    "action": "destroy",
    "destroy_targets": [
      {"type": "google_compute_instance", "name": "test_vm_01"},
      {"type": "google_compute_instance", "name": "test_vm_02"}
    ],
    "message": "<explanation>"
  }
- "destroy_targets" is a LIST — always a list even for a single resource
- Special patterns:
    "remove all resources" / "destroy everything"  → destroy_targets = [{"type":"__ALL__","name":"__ALL__"}]
    "remove all vms" / "destroy all instances"     → destroy_targets = [{"type":"google_compute_instance","name":"__TYPE_ALL__"}]
    "remove all buckets"                           → destroy_targets = [{"type":"google_storage_bucket","name":"__TYPE_ALL__"}]
    "remove bucket X and Y"                        → destroy_targets with name=X and name=Y
- Search BOTH main.tf AND the tfstate summary for resource names
- If a resource is NOT found anywhere: return action="info" listing what exists
- If found in a DIFFERENT region than user requested: return action="info" asking confirmation
- NEVER modify or rewrite main.tf for a destroy — only return destroy_targets
- NEVER return "hcl" for destroy actions — leave it as empty string ""

## RULES FOR apply_target action
- Use when user says "only apply X", "just apply X", "apply only X", "apply X not the rest",
  "create X with apply it only", "create X and apply it only", "with apply it only"
- In these cases: set action="apply_target", NOT "write"
- Set apply_targets to list of {type, name} for ONLY the named new resources
- Your "hcl" field must contain ONLY the new resource block(s) — same rule as write action
- In message, confirm what will be applied and what will be skipped

## RULES FOR fix_security action
- Use when user says "fix security", "solve security issues", "fix security issues",
  "apply security fixes", "fix HIGH issues", "fix VM-001", "fix all security", or any similar intent
- Set action="fix_security"
- Set fix_ids to list of rule IDs to fix, e.g. ["VM-001","FW-001"] — or ["ALL"] to fix all findings
- Leave hcl as "" — the engine will apply patches programmatically
- In message, describe what will be fixed and what requires manual action

## CRITICAL
- Output ONLY valid JSON. No markdown. No explanation outside JSON.
- HCL must be 100% valid terraform
- NEVER return action="info" for a resource creation request — ALWAYS return action="write" with the HCL
- NEVER tell the user to "run terraform apply in your terminal" — the UI handles apply automatically
- If you can generate HCL for the request, do it — don't ask clarifying questions, make reasonable defaults
"""



def call_gemini(messages: list, current_hcl: str, jid: str,
                error_feedback: str = "") -> dict:
    """Call Gemini API and return parsed decision dict."""
    genai.configure(api_key=GEMINI_API_KEY)

    provider_aliases = get_existing_provider_aliases()
    alias_info = "\n".join(
        f'  alias="{a}" region="{r}"' for a, r in provider_aliases.items()
    ) if provider_aliases else "  (none — default region only)"

    context  = f"\n\nCurrent main.tf:\n```hcl\n{current_hcl}\n```" if current_hcl \
               else "\n\nCurrent main.tf: empty"
    context += f"\n\nAvailable provider aliases in provider.tf:\n{alias_info}"
    context += f"\n\nDefault region: {GCP_DEFAULT_REGION} (no alias needed for this region)"
    context += f"\n\nGCP Project ID: {GCP_PROJECT_ID}"
    # Inject live tfstate so Gemini knows what's actually applied in GCP
    context += f"\n\n{get_tfstate_summary_text()}"

    if error_feedback:
        context += f"\n\nTerraform validation ERROR — fix this:\n{error_feedback}"

    # Build conversation history as a single prompt string
    parts = []
    for m in messages:
        role_tag = "User" if m["role"] == "user" else "Assistant"
        parts.append(f"{role_tag}: {m['content']}")

    if parts:
        parts[-1] += context   # Append context to the last user message

    full_prompt = "\n\n".join(parts)

    _log(jid, "🤖 Calling Gemini API...")

    model_client = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
        generation_config=genai.GenerationConfig(
            max_output_tokens=8192,
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )

    response = model_client.generate_content(full_prompt)
    raw = response.text.strip()
    raw = re.sub(r'```json|```', '', raw).strip()

    try:
        usage = response.usage_metadata
        _log(jid, f"📊 Tokens: {usage.prompt_token_count} in / "
                  f"{usage.candidates_token_count} out")
    except Exception:
        pass

    _log(jid, f"📨 Gemini response: {raw[:150]}")
    return json.loads(raw)

# ---------------------------------------------------------------------------
# Agent worker — with validation retry loop
# ---------------------------------------------------------------------------
def _agent_worker(jid: str, messages: list):
    try:
        latest_msg = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        detected_region = extract_region_from_prompt(latest_msg)

        # ── Keyword fast-path: drift detection ───────────────────────────────
        # Intercept obvious drift phrases before calling Gemini — avoids the
        # model routing them to the generic "query" action.
        _drift_keywords = (
            "check drift", "check infrastructure drift", "detect drift",
            "infrastructure drift", "show drift", "drift detection",
            "terraform drift", "state drift", "infra drift",
            "has anything changed", "what changed outside terraform",
            "is my infrastructure in sync", "sync check",
            "refresh-only", "plan refresh",
        )
        _msg_lower = latest_msg.lower().strip()
        if any(kw in _msg_lower for kw in _drift_keywords):
            _log(jid, "🔍 Drift keyword detected — running terraform plan -refresh-only…")
            drift = detect_drift(jid)
            _finish(jid, {
                "status":  "drift",
                "drift":   drift,
                "message": drift.get("summary", ""),
            })
            return
        # ─────────────────────────────────────────────────────────────────────

        if detected_region:
            if detected_region != GCP_DEFAULT_REGION:
                ensure_provider_alias(detected_region, jid)
            else:
                _log(jid, f"ℹ️ Region {detected_region} is default — no alias needed")
        else:
            _log(jid, "ℹ️ No specific region detected — using default")

        current_hcl    = TerraformTools.read_code()
        error_feedback = ""
        max_retries    = 3

        for attempt in range(1, max_retries + 1):
            _log(jid, f"🔄 Attempt {attempt}/{max_retries}")
            try:
                decision = call_gemini(messages, current_hcl, jid, error_feedback)
            except json.JSONDecodeError as e:
                _error(jid, f"❌ Gemini returned invalid JSON: {e}")
                return
            except Exception as e:
                _error(jid, f"❌ Gemini API error: {e}")
                return

            action = decision.get("action", "info")
            _log(jid, f"🛠️  action='{action}'")

            # ── Destroy ──────────────────────────────────────────────────────
            if action == "destroy":

                # Resolve destroy_targets (new) or fall back to legacy resource_name
                raw_targets = decision.get("destroy_targets") or []
                if not raw_targets:
                    # Legacy: single resource_name / resource_type
                    rn = decision.get("resource_name", "")
                    rt = decision.get("resource_type", "")
                    if rn:
                        raw_targets = [{"type": rt, "name": rn}]

                if not raw_targets:
                    _finish(jid, {"status": "info", "message": decision.get("message",
                                  "⚠️ No resources specified to destroy.")})
                    return

                # ── Resolve __ALL__ / __TYPE_ALL__ patterns ───────────────────
                is_all      = any(t.get("name") == "__ALL__" for t in raw_targets)
                type_filter = None  # e.g. "google_compute_instance"
                if not is_all:
                    type_all = next((t for t in raw_targets if t.get("name") == "__TYPE_ALL__"), None)
                    if type_all:
                        type_filter = type_all.get("type")

                current_code = TerraformTools.read_code()
                all_res = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', current_code)
                # all_res = [(type, name), ...]

                if is_all:
                    resolved = [{"type": t, "name": n} for t, n in all_res]
                elif type_filter:
                    resolved = [{"type": t, "name": n} for t, n in all_res if t == type_filter]
                else:
                    # Named targets — verify each exists
                    resolved = []
                    missing  = []
                    for tgt in raw_targets:
                        found_res = get_resource_info_from_hcl(tgt["name"])
                        if found_res:
                            resolved.append({"type": found_res["type"], "name": tgt["name"]})
                        else:
                            missing.append(tgt["name"])
                    if missing:
                        all_names = _list_all_resource_names()
                        _finish(jid, {"status": "info", "message": (
                            f"⚠️ Not found: `{'`, `'.join(missing)}`.\n\n"
                            f"Available: {', '.join(f'`{n}`' for n in all_names) or 'none'}"
                        )})
                        return

                if not resolved:
                    _finish(jid, {"status": "info", "message":
                        "⚠️ No matching resources found to destroy."})
                    return

                # ── Build terraform plan -destroy with -target flags ───────────
                _log(jid, f"🎯 Destroy plan for: {[r['name'] for r in resolved]}")
                init = TerraformTools.run_init(jid)
                if init != "ok":
                    _error(jid, f"❌ Init failed: {init}"); return

                if is_all:
                    # Full destroy — no targets needed
                    target_flags = []
                else:
                    target_flags = [f"-target={r['type']}.{r['name']}" for r in resolved]

                import subprocess as _sp
                res_plan = _sp.run(
                    ["terraform", "plan", "-destroy", "-no-color"] + target_flags,
                    cwd=TF_DIR, capture_output=True, text=True, timeout=120)
                plan = res_plan.stdout
                if res_plan.returncode not in (0, 2):
                    plan += f"\n❌ STDERR:\n{res_plan.stderr}"
                _log(jid, "✅ Destroy plan complete.")

                plan_details = parse_plan_details(plan, current_code)

                names_label = ", ".join(f"`{r['name']}`" for r in resolved)
                _finish(jid, {
                    "status": "success",
                    "code": current_code,
                    "plan": plan,
                    "plan_details": plan_details,
                    "message": decision.get("message") or
                               f"Ready to destroy: {names_label}",
                    "is_destroy": True,
                    "destroy_target": {
                        "type":    "__ALL__" if is_all else (type_filter or resolved[0]["type"]),
                        "name":    "__ALL__" if is_all else ("__TYPE_ALL__" if type_filter else resolved[0]["name"]),
                        "targets": resolved,   # full list for multi-destroy
                    },
                })
                return

            # ── Write ─────────────────────────────────────────────────────────
            if action == "write":
                hcl = decision.get("hcl", "").strip()
                hcl = re.sub(r'```(?:hcl|terraform)?|```', '', hcl).strip()
                if not hcl:
                    _error(jid, "❌ Gemini returned empty HCL.")
                    return

                # Snapshot current main.tf before any changes — restored on plan expiry/cancel
                if attempt == 1:
                    TerraformTools.snapshot_main_tf()

                write_result = TerraformTools.write_code(hcl)
                if write_result.startswith("DUPLICATE:"):
                    dup_keys = write_result[len("DUPLICATE:"):].split(",")
                    _finish(jid, {
                        "status": "info",
                        "message": (
                            f"ℹ️ Resource(s) already exist in main.tf: `{'`, `'.join(dup_keys)}`.\n\n"
                            f"Use a different name or ask to destroy the existing one first."
                        )
                    })
                    return

                _log(jid, "📝 Appended new block(s) to main.tf.")
                val = TerraformTools.run_validate(jid)
                if val != "ok":
                    _log(jid, f"⚠️ Validation failed (attempt {attempt}): {val[:200]}")
                    if attempt < max_retries:
                        error_feedback = val
                        current_hcl    = hcl
                        continue
                    else:
                        _error(jid,
                               f"❌ Validation failed after {max_retries} attempts:\n{val}")
                        return

                plan = TerraformTools.run_plan(jid)
                current_code = TerraformTools.read_code()
                plan_details = parse_plan_details(plan, current_code)

                # Cost estimation + security audit (read-only — no auto-patching)
                cost_estimate  = estimate_plan_cost(plan_details, current_code, jid)
                security_audit = audit_security(current_code, plan_details, jid)
                if cost_estimate["total_monthly"] > 0:
                    _log(jid, f"💰 Estimated cost: ${cost_estimate['total_monthly']:.2f}/month")
                if security_audit:
                    high = sum(1 for f in security_audit if f["severity"] == "HIGH")
                    _log(jid, f"🔒 Security: {len(security_audit)} finding(s), {high} HIGH — use 'fix security issues' to patch")

                # Detect unexpected destroys in a write action → warn user
                has_collateral_destroy = bool(
                    re.search(r'[1-9]\d* to destroy', plan))

                _finish(jid, {
                    "status":          "success",
                    "code":            current_code,
                    "plan":            plan,
                    "plan_details":    plan_details,
                    "cost_estimate":   cost_estimate,
                    "security_audit":  security_audit,
                    "auto_fixed":      [],
                    "message":         decision.get("message", "Plan ready."),
                    "is_destroy":      False,
                    "collateral_warning": ["⚠️ Plan unexpectedly destroys existing resources"]
                                          if has_collateral_destroy else [],
                })
                return

            # ── Apply target: write HCL → targeted plan → targeted apply ─────
            if action == "apply_target":
                targets = decision.get("apply_targets", [])
                if not targets:
                    _finish(jid, {"status": "info",
                                  "message": "⚠️ No targets specified for targeted apply."})
                    return

                # Write HCL if provided (new blocks only — appended to main.tf)
                hcl = decision.get("hcl", "").strip()
                hcl = re.sub(r'```(?:hcl|terraform)?|```', '', hcl).strip()
                if hcl:
                    write_result = TerraformTools.write_code(hcl)
                    if write_result.startswith("DUPLICATE:"):
                        dup_keys = write_result[len("DUPLICATE:"):].split(",")
                        _finish(jid, {"status": "info",
                                      "message": f"ℹ️ Resource(s) already in main.tf: `{'`, `'.join(dup_keys)}`."})
                        return
                    val = TerraformTools.run_validate(jid)
                    if val != "ok":
                        if attempt < max_retries:
                            error_feedback = val; continue
                        _error(jid, f"❌ Validation failed:\n{val}"); return
                    _log(jid, "📝 Appended new block(s) to main.tf.")

                _log(jid, f"🎯 Targeted plan: {[t['type']+'.'+t['name'] for t in targets]}")
                target_flags = [f"-target={t['type']}.{t['name']}" for t in targets]
                init = TerraformTools.run_init(jid)
                if init != "ok":
                    _error(jid, f"❌ Init failed:\n{init}"); return
                res = subprocess.run(
                    ["terraform", "plan", "-no-color", "-input=false"] + target_flags,
                    cwd=TF_DIR, capture_output=True, text=True, timeout=120)
                plan_out = res.stdout + res.stderr
                if res.returncode != 0:
                    _error(jid, f"❌ Targeted plan failed:\n{plan_out}"); return

                current_code = TerraformTools.read_code()
                plan_details = parse_plan_details(plan_out, current_code)
                names = ", ".join(f"{t['type']}.{t['name']}" for t in targets)
                _finish(jid, {
                    "status":        "success",
                    "plan":          plan_out,
                    "plan_details":  plan_details,
                    "is_destroy":    False,
                    "is_targeted":   True,
                    "apply_targets": targets,
                    "message":       decision.get("message",
                                     f"🎯 Targeted plan ready — will apply ONLY: {names}"),
                })
                return

            # ── Fix Security: patch HCL for requested rule IDs → re-plan ──────
            if action == "fix_security":
                fix_ids  = decision.get("fix_ids", ["ALL"])
                fix_all  = "ALL" in fix_ids

                current_code = TerraformTools.read_code()
                if not current_code:
                    _finish(jid, {"status": "info",
                                  "message": "ℹ️ No main.tf to fix — nothing deployed yet."})
                    return

                # Build plan_details-like dict covering all resources in current HCL
                all_resources = [
                    {"name": k.split(".")[1], "type": k.split(".")[0]}
                    for k in TerraformTools.extract_resource_blocks(current_code)
                ]
                all_pd = {"add": all_resources, "replace": []}

                # Get current findings
                findings = audit_security(current_code, all_pd, jid)
                if not findings:
                    _finish(jid, {"status": "info",
                                  "message": "✅ No security issues found in current HCL."})
                    return

                # Filter to requested IDs
                if not fix_all:
                    findings = [f for f in findings if f["id"] in fix_ids]
                    if not findings:
                        _finish(jid, {"status": "info",
                                      "message": f"ℹ️ None of {fix_ids} matched current findings."})
                        return

                # Apply patches — pass fix_ids so Gemini only fixes the requested ones
                target_ids = None if fix_all else fix_ids
                fixed_hcl, applied = auto_fix_security(
                    current_code, all_pd, jid, fix_ids=target_ids)

                if not applied:
                    unfixable = [f["id"] for f in findings]
                    _finish(jid, {"status": "info",
                                  "message": (f"⚠️ {unfixable} have no auto-patch available — "
                                              f"these require manual HCL edits. "
                                              f"See the Fix guidance in each finding.")})
                    return

                # Write + validate — fix_security rewrites the FULL file (patching existing blocks)
                tf_path = f"{TF_DIR}/main.tf"
                with open(tf_path, "w") as f:
                    f.write(fixed_hcl.strip() + "\n")

                val = TerraformTools.run_validate(jid)
                if val != "ok":
                    # Revert to snapshot
                    with open(tf_path, "w") as f:
                        f.write(current_code)
                    _error(jid, f"❌ Security patch failed validation — reverted.\n{val}")
                    return

                fixed_ids = [f["id"] for f in applied]
                _log(jid, f"🔒 Patched: {fixed_ids} — re-running plan")

                plan         = TerraformTools.run_plan(jid)
                current_code = TerraformTools.read_code()
                plan_details = parse_plan_details(plan, current_code)
                cost_est     = estimate_plan_cost(plan_details, current_code, jid)
                # Re-audit to show remaining findings
                remaining    = audit_security(current_code, {"add": all_resources, "replace": []}, jid)
                remaining_unfixed = [f for f in remaining if f["id"] not in fixed_ids]

                _finish(jid, {
                    "status":         "success",
                    "plan":           plan,
                    "plan_details":   plan_details,
                    "cost_estimate":  cost_est,
                    "security_audit": remaining_unfixed,
                    "auto_fixed":     applied,
                    "is_destroy":     False,
                    "message":        (
                        decision.get("message") or
                        f"🔒 Fixed {len(applied)} issue(s): {fixed_ids}. "
                        + (f"{len(remaining_unfixed)} finding(s) still need manual review."
                           if remaining_unfixed else "No remaining security issues ✅")
                    ),
                    "collateral_warning": [],
                })
                return

            # ── Check drift — runs terraform plan -refresh-only ───────────────
            if action == "check_drift":
                _log(jid, "🔍 Running drift detection…")
                drift = detect_drift(jid)
                _finish(jid, {
                    "status":  "drift",
                    "drift":   drift,
                    "message": drift.get("summary", ""),
                })
                return

            # ── Query — live GCP observability ────────────────────────────
            if action == "query":
                q = decision.get("query") or {}
                _log(jid, f"🔍 Query: {q.get('type')} — {q.get('resource_name','')}")
                answer = _run_gcp_query(q, jid)
                _finish(jid, {
                    "status":     "query",
                    "message":    answer,
                    "query_meta": q,
                })
                return

            # ── Deploy — repo → analyse → HCL → plan → ready to apply ────
            if action == "deploy":
                d = decision.get("deploy") or {}
                repo_url = d.get("repo_url", "")
                if not repo_url:
                    _finish(jid, {"status": "info",
                                  "message": "❌ No repository URL found. Please provide the full URL, e.g. https://github.com/owner/repo"})
                    return
                _log(jid, f"🚀 Starting deployment from {repo_url}")
                deploy_result = _run_repo_deploy(
                    repo_url    = repo_url,
                    branch      = d.get("branch", "main"),
                    app_name    = d.get("app_name", ""),
                    environment = d.get("environment", "production"),
                    user_prompt = d.get("prompt", ""),
                    jid         = jid,
                    user        = JOBS[jid].get("_user", "unknown"),
                )
                if deploy_result.get("error"):
                    _error(jid, deploy_result["error"])
                    return
                # deploy engine wrote HCL to main.tf and ran plan
                plan         = deploy_result["plan"]
                current_code = TerraformTools.read_code()
                plan_details = parse_plan_details(plan, current_code)
                cost_est     = estimate_plan_cost(plan_details, current_code, jid)
                security     = audit_security(current_code, plan_details, jid)
                _finish(jid, {
                    "status":         "success",
                    "plan":           plan,
                    "plan_details":   plan_details,
                    "cost_estimate":  cost_est,
                    "security_audit": security,
                    "auto_fixed":     [],
                    "is_destroy":     False,
                    "deploy_meta":    deploy_result.get("meta", {}),
                    "message":        deploy_result.get("message", "Deployment plan ready."),
                    "collateral_warning": [],
                })
                return

            # ── Info ──────────────────────────────────────────────────────────
            _finish(jid, {
                "status": "info",
                "message": decision.get("message", "No changes needed."),
            })
            return

    except Exception as e:
        _error(jid, f"❌ Exception: {e}")


def _apply_worker(jid: str, is_destroy: bool = False, destroy_target: dict = None,
                  apply_targets: list = None):
    try:
        if is_destroy and destroy_target:
            # ── Destroy — supports single, multi, type-all, or all ────────────
            d_name    = destroy_target.get("name", "")
            d_type    = destroy_target.get("type", "")
            d_targets = destroy_target.get("targets", [])  # list from new handler

            if d_name == "__ALL__":
                # Full destroy
                out = TerraformTools.run_destroy_all(jid)
                if out.startswith("❌"):
                    _error(jid, out); return
                path = f"{TF_DIR}/main.tf"
                if os.path.exists(path): os.remove(path)

            elif d_targets:
                # Multi-target destroy — run apply -destroy with -target flags
                target_flags = [f"-target={t['type']}.{t['name']}" for t in d_targets]
                _log(jid, f"💣 Destroying {len(d_targets)} resource(s)...")
                init = TerraformTools.run_init(jid, force=True)
                if init != "ok":
                    _error(jid, f"❌ Init failed:\n{init}"); return
                res = subprocess.run(
                    ["terraform", "apply", "-destroy", "-auto-approve", "-no-color"] + target_flags,
                    cwd=TF_DIR, capture_output=True, text=True, timeout=600)
                out = res.stdout + res.stderr
                if res.returncode != 0 and "destroy complete" not in out.lower():
                    _error(jid, f"❌ Destroy failed:\n{out}"); return
                # Remove all destroyed blocks from main.tf
                if d_name == "__TYPE_ALL__":
                    TerraformTools.remove_resource_blocks(resource_type=d_type)
                else:
                    names_to_remove = [t["name"] for t in d_targets]
                    TerraformTools.remove_resource_blocks(names=names_to_remove)

            else:
                # Single target (legacy path)
                out = TerraformTools.run_destroy_target(jid, d_type, d_name)
                if out.startswith("❌"):
                    _error(jid, out); return
                TerraformTools.remove_resource_block(d_name)

            TerraformTools.clear_snapshot()
            TerraformTools.save_version(
                label=f"destroy: {d_name if d_name not in ('__ALL__','__TYPE_ALL__') else ('all resources' if d_name=='__ALL__' else f'all {d_type}')}",
                user=JOBS[jid].get("_user", "system"), action="destroy")
            _push_main_tf_to_gcs(jid)
            _redis_publish("destroy:done", {
                "job_id": jid, "user": JOBS[jid].get("_user", "system"),
                "destroyed": [t["name"] for t in (d_targets or [{"name": d_name}])],
            })
            _finish(jid, {"status": "success", "output": out, "destroyed": True})

        elif apply_targets:
            # ── Targeted apply — only specified resources ─────────────────────
            names = ", ".join(f"{t['type']}.{t['name']}" for t in apply_targets)
            _log(jid, f"🎯 Applying only: {names}")
            out = TerraformTools.run_apply_targets(jid, apply_targets)
            if out.startswith("❌"):
                _error(jid, out)
                return
            TerraformTools.clear_snapshot()
            TerraformTools.save_version(
                label=f"apply-target: {names}",
                user=JOBS[jid].get("_user", "system"), action="apply_target")
            _push_main_tf_to_gcs(jid)
            _redis_publish("apply:done", {
                "job_id":      jid,
                "user":        JOBS[jid].get("_user", "system"),
                "apply_output": out,
                "resources": [{"type": t["type"], "name": t["name"]} for t in apply_targets],
                "resource_type": apply_targets[0]["type"] if apply_targets else "",
                "resource_name": apply_targets[0]["name"] if apply_targets else "",
            })
            _finish(jid, {"status": "success", "output": out, "destroyed": False,
                          "targeted": True, "target_names": names})

        else:
            # ── Full apply ────────────────────────────────────────────────────
            _log(jid, "🚀 Running apply...")
            init = TerraformTools.run_init(jid, force=True)
            if init != "ok":
                _error(jid, f"❌ Init failed:\n{init}")
                return
            out = TerraformTools.run_apply(jid)
            if out.startswith("❌"):
                _error(jid, out)
                return
            if "no changes" in out.lower() and "apply complete" not in out.lower():
                TerraformTools.clear_snapshot()
                _finish(jid, {"status": "info",
                              "message": "ℹ️ No changes — all resources already match GCP state."})
                return
            TerraformTools.clear_snapshot()
            TerraformTools.save_version(
                label="apply",
                user=JOBS[jid].get("_user", "system"), action="apply")
            _push_main_tf_to_gcs(jid)
            # Publish apply:done for A7 UI Navigator to verify
            current_code = TerraformTools.read_code()
            resources = [{"type": t, "name": n}
                         for t, n in re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', current_code)]
            _redis_publish("apply:done", {
                "job_id":      jid,
                "user":        JOBS[jid].get("_user", "system"),
                "apply_output": out,
                "resources":   resources,
                "resource_type": resources[0]["type"] if resources else "",
                "resource_name": resources[0]["name"] if resources else "",
            })
            _finish(jid, {"status": "success", "output": out, "destroyed": False})

    except Exception as e:
        _error(jid, f"❌ Apply exception: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_version_history() -> list[dict]:
    """Return version history list (no HCL content, just metadata)."""
    return TerraformTools.list_versions()


def start_rollback_job(version_id: str, user: str = "unknown") -> str:
    """
    Restore main.tf from a saved version, run plan, return result for user confirmation.
    The user still needs to click Apply to actually change GCP state.
    """
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "rollback"
        JOBS[jid]["_user"]     = user

    def _worker():
        try:
            entry = TerraformTools.get_version(version_id)
            if not entry:
                _error(jid, f"❌ Version `{version_id}` not found.")
                return

            _log(jid, f"⏪ Restoring version {version_id} ({entry.get('ts','')[:19]})")
            _log(jid, f"   Label: {entry.get('label','—')}  |  "
                      f"Resources: {entry.get('resource_count', '?')}  |  "
                      f"State serial: {entry.get('state_serial','?')}")

            # Restore main.tf (snapshots current first)
            result = TerraformTools.restore_version(version_id)
            if result != "ok":
                _error(jid, result); return

            # Validate the restored HCL
            _log(jid, "🔍 Validating restored configuration...")
            val = TerraformTools.run_validate(jid)
            if val != "ok":
                TerraformTools.revert_to_snapshot()
                _error(jid, f"❌ Restored HCL failed validation — reverted:\n{val}")
                return

            # Run plan to show what will change
            _log(jid, "📋 Planning rollback changes...")
            init = TerraformTools.run_init(jid)
            if init != "ok":
                TerraformTools.revert_to_snapshot()
                _error(jid, f"❌ Init failed — reverted:\n{init}"); return

            plan = TerraformTools.run_plan(jid)
            current_code = TerraformTools.read_code()
            plan_details = parse_plan_details(plan, current_code)

            cost_estimate  = estimate_plan_cost(plan_details, current_code, jid)
            security_audit = audit_security(current_code, plan_details, jid)

            _log(jid, f"✅ Rollback plan ready — review and apply to complete rollback")
            _finish(jid, {
                "status":        "success",
                "plan":          plan,
                "plan_details":  plan_details,
                "code":          current_code,
                "cost_estimate": cost_estimate,
                "security_audit":security_audit,
                "is_rollback":   True,
                "rollback_version": entry,
                "message": (
                    f"⏪ **Rollback plan ready** — version `{version_id}` "
                    f"({entry.get('label','')}, {entry.get('resource_count',0)} resources).\n\n"
                    f"Review the plan above and click **Apply** to complete the rollback."
                ),
            })

        except Exception as exc:
            _error(jid, f"❌ Rollback failed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()
    return jid


def _push_main_tf_to_gcs(jid: str = None):
    """
    Upload main.tf (and provider.tf, backend.tf) to GCS after every apply.
    This ensures drift detection can restore config files on Cloud Run cold starts.
    Files saved under the same bucket as tfstate, at root level.
    """
    state_bucket = os.environ.get("TF_STATE_BUCKET", "")
    if not state_bucket:
        return
    try:
        from google.cloud import storage as _gcs
        client = _gcs.Client()
        bucket = client.bucket(state_bucket)
        pushed = []
        for fname in ("main.tf", "provider.tf", "backend.tf"):
            fpath = f"{TF_DIR}/{fname}"
            if os.path.exists(fpath) and open(fpath).read().strip():
                bucket.blob(fname).upload_from_filename(
                    fpath, content_type="text/plain")
                pushed.append(fname)
        if pushed and jid:
            _log(jid, f"☁️  Config files saved to GCS: {', '.join(pushed)}")
        elif jid:
            _log(jid, "⚠️  No config files to push to GCS")
    except Exception as exc:
        if jid:
            _log(jid, f"⚠️  GCS config push failed: {exc}")
        else:
            print(f"[gcs_push] {exc}")


def start_state_push_job() -> str:
    """Manually push local state → GCS and verify lock is clear."""
    jid = _new_job()
    def _worker():
        try:
            state_bucket = os.environ.get("TF_STATE_BUCKET", "")
            if not state_bucket:
                _error(jid, "❌ TF_STATE_BUCKET is not set — cannot push state.")
                return
            _log(jid, f"☁️  Pushing state → gs://{state_bucket}...")
            # Check for existing lock first
            lock = get_state_lock_info()
            if lock:
                _error(jid, (
                    f"❌ State is locked by {lock['who']} (op: {lock['operation']})\n"
                    f"Lock ID: {lock['id']}\nCreated: {lock['when']}\n\n"
                    f"Resolve with: terraform force-unlock {lock['id']}"
                ))
                return
            # terraform state push uploads local state to remote
            local_state = f"{TF_DIR}/terraform.tfstate"
            if not os.path.exists(local_state):
                # Try pulling from remote — if that works, state is already there
                res = subprocess.run(["terraform", "state", "pull"],
                    cwd=TF_DIR, capture_output=True, text=True, timeout=30)
                if res.returncode == 0 and res.stdout.strip():
                    _finish(jid, {"status": "success",
                                  "message": "✅ Remote state is up to date (no local state to push)."})
                else:
                    _error(jid, "❌ No local terraform.tfstate found to push.")
                return
            res = subprocess.run(
                ["terraform", "state", "push", local_state],
                cwd=TF_DIR, capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                _error(jid, f"❌ State push failed:\n{res.stdout + res.stderr}")
                return
            # Invalidate cache
            global _STATE_CACHE
            _STATE_CACHE = {"ts": 0, "data": None}
            _log(jid, f"✅ State pushed → gs://{state_bucket}")
            _finish(jid, {"status": "success",
                          "message": f"✅ State pushed to gs://{state_bucket}"})
        except Exception as e:
            _error(jid, f"❌ State push error: {e}")
    threading.Thread(target=_worker, daemon=True).start()
    return jid


def start_diagram_job(img_bytes: bytes | None, img_mime: str,
                      img_url: str | None, extra: str, messages: list) -> str:
    """
    Analyse an architecture diagram (uploaded file or URL) with Gemini Vision
    and generate Terraform HCL from it. Injects the resulting HCL into the
    normal agent pipeline (validate → plan) just like a text prompt would.
    """
    jid = _new_job()

    def _worker():
        try:
            _log(jid, "🖼️  Loading diagram image...")
            import urllib.request as _ur
            import base64 as _b64

            # Resolve image bytes
            raw_bytes = img_bytes
            mime      = img_mime or "image/png"

            if not raw_bytes and img_url:
                # Fetch from URL
                try:
                    req = _ur.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                    with _ur.urlopen(req, timeout=15) as resp:
                        raw_bytes = resp.read()
                    # Guess mime from URL extension
                    ext = img_url.rsplit(".", 1)[-1].lower()
                    mime = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                            "png":"image/png","webp":"image/webp"}.get(ext, "image/png")
                except Exception as e:
                    _error(jid, f"❌ Could not fetch image from URL: {e}")
                    return

            if not raw_bytes:
                _error(jid, "❌ No image provided.")
                return

            _log(jid, f"🤖 Sending diagram to Gemini Vision ({len(raw_bytes)//1024}KB)...")
            genai.configure(api_key=GEMINI_API_KEY)

            # Build a rich prompt asking Gemini to describe and plan the architecture
            extra_hint = f"\n\nExtra instructions: {extra}" if extra else ""
            state_hint = get_tfstate_summary_text()
            current_hcl = TerraformTools.read_code()
            hcl_hint = f"\n\nExisting main.tf:\n```hcl\n{current_hcl}\n```" if current_hcl else ""

            vision_prompt = (
                f"You are a GCP infrastructure expert. Analyse the architecture diagram in this image.\n"
                f"Identify every GCP resource shown (VMs, networks, subnets, GKE clusters, buckets, "
                f"load balancers, Cloud SQL, Pub/Sub, etc.) and their relationships.\n\n"
                f"Then generate complete, production-ready Terraform HCL for all resources shown.\n\n"
                f"Rules:\n"
                f"- Use provider \"google\" only\n"
                f"- Default region: {GCP_DEFAULT_REGION}, project: {GCP_PROJECT_ID}\n"
                f"- Name resources clearly from what is shown in the diagram\n"
                f"- Include all required fields; omit optional fields unless shown\n"
                f"- Output ONLY a JSON object with these keys:\n"
                f"  {{\n"
                f"    \"description\": \"1-2 sentence summary of the architecture\",\n"
                f"    \"resources_found\": [\"list of resource names/types seen\"],\n"
                f"    \"hcl\": \"complete terraform HCL string\"\n"
                f"  }}\n"
                f"\n{state_hint}{hcl_hint}{extra_hint}"
            )

            vision_model = genai.GenerativeModel(
                model_name=GEMINI_MODEL,
                generation_config=genai.GenerationConfig(
                    max_output_tokens=8192,
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )

            img_part = {"mime_type": mime, "data": raw_bytes}
            response = vision_model.generate_content([vision_prompt, img_part])
            raw = response.text.strip()
            raw = re.sub(r'```json|```', '', raw).strip()

            try:
                usage = response.usage_metadata
                _log(jid, f"📊 Tokens: {usage.prompt_token_count} in / "
                          f"{usage.candidates_token_count} out")
            except Exception:
                pass

            result = json.loads(raw)
            description = result.get("description", "Architecture from diagram")
            hcl          = result.get("hcl", "").strip()
            found        = result.get("resources_found", [])

            if not hcl:
                _error(jid, "❌ Gemini could not extract Terraform from the diagram.")
                return

            _log(jid, f"✅ Found: {', '.join(found[:6])}")
            _log(jid, f"📝 Writing HCL ({len(hcl)} chars)...")

            # Write HCL to main.tf (merge with existing, overwrite for simplicity)
            path = f"{TF_DIR}/main.tf"
            existing = TerraformTools.read_code()
            if existing.strip():
                # Append new resources, don't overwrite
                with open(path, "a") as f:
                    f.write("\n# --- Resources from diagram ---\n")
                    f.write(hcl)
            else:
                with open(path, "w") as f:
                    f.write(hcl)

            # Run validate + plan via normal agent pipeline
            _log(jid, "🔍 Validating generated HCL...")
            validate_result = TerraformTools.run_validate(jid)
            if validate_result != "ok":
                _error(jid, f"❌ Diagram HCL validation failed:\n{validate_result}")
                return

            _log(jid, "📋 Running terraform plan...")
            plan_out = TerraformTools.run_plan(jid)
            if plan_out.startswith("❌"):
                _error(jid, plan_out)
                return

            plan_details = parse_plan_details(plan_out, TerraformTools.read_code())

            # Inject a user message explaining what was found
            diag_msg = (f"[Diagram] {description} "
                        f"Resources: {', '.join(found[:8])}")

            _finish(jid, {
                "status":       "success",
                "plan":         plan_out,
                "plan_details": plan_details,
                "is_destroy":   False,
                "message":      diag_msg,
            })

        except json.JSONDecodeError as e:
            _error(jid, f"❌ Gemini returned invalid JSON: {e}")
        except Exception as e:
            _error(jid, f"❌ Diagram job failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return jid




# ---------------------------------------------------------------------------
# GCP Observability Query Engine
# Uses GCP REST APIs directly (requests + SA JWT) — no gcloud CLI needed.
# Covers: VMs, buckets, logs, Cloud Run, billing, metrics.
# ---------------------------------------------------------------------------

def _gcp_access_token() -> str:
    """
    Get a GCP access token from the service account JSON.
    Uses JWT + google OAuth2 token endpoint via requests only.
    """
    import time, json as _json, hashlib, hmac, struct
    cred_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/app/gcp-credentials.json")
    raw_creds = os.environ.get("GOOGLE_CREDENTIALS", "")

    try:
        if raw_creds:
            sa = _json.loads(raw_creds)
        elif os.path.exists(cred_file):
            with open(cred_file) as f:
                sa = _json.load(f)
        else:
            return ""

        # Build JWT using only stdlib (no google-auth)
        import base64 as _b64, urllib.request, urllib.parse

        def _b64url(data: bytes) -> str:
            return _b64.urlsafe_b64encode(data).rstrip(b"=").decode()

        now    = int(time.time())
        header = _b64url(_json.dumps({"alg":"RS256","typ":"JWT"}).encode())
        claim  = _b64url(_json.dumps({
            "iss":   sa["client_email"],
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "aud":   "https://oauth2.googleapis.com/token",
            "iat":   now,
            "exp":   now + 3600,
        }).encode())

        # Sign with RSA-SHA256 using cryptography lib (ships with most Python envs)
        try:
            from cryptography.hazmat.primitives import serialization, hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.backends import default_backend

            private_key = serialization.load_pem_private_key(
                sa["private_key"].encode(), password=None, backend=default_backend())
            sig_input = f"{header}.{claim}".encode()
            sig = private_key.sign(sig_input, padding.PKCS1v15(), hashes.SHA256())
            jwt_token = f"{header}.{claim}.{_b64url(sig)}"
        except ImportError:
            return ""   # cryptography not available

        # Exchange JWT for access token
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion":  jwt_token,
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = urllib.request.urlopen(req, timeout=10)
        return _json.loads(resp.read()).get("access_token", "")

    except Exception as exc:
        print(f"[auth] token error: {exc}")
        return ""


def _gcp_get(path: str, token: str, params: dict = None) -> dict:
    """GET a GCP REST API endpoint."""
    import urllib.request, urllib.parse, json as _json
    url = f"https://compute.googleapis.com{path}" if path.startswith("/compute") else \
          f"https://logging.googleapis.com{path}" if path.startswith("/logging") else \
          f"https://storage.googleapis.com{path}" if path.startswith("/storage") else \
          f"https://run.googleapis.com{path}"     if path.startswith("/run") else \
          f"https://cloudresourcemanager.googleapis.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return _json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


def _run_gcp_query(q: dict, jid: str) -> str:
    """
    Execute a live GCP query via REST API and return a Gemini-synthesised answer.
    No gcloud CLI or google-cloud-* SDK required.
    """
    project  = GCP_PROJECT_ID
    qtype    = q.get("type", "generic")
    name     = (q.get("resource_name") or "").strip()
    metric   = q.get("metric", "cpu")
    minutes  = int(q.get("minutes", 60))
    log_filter = q.get("filter", "")

    raw_data  = {}
    token     = _gcp_access_token()

    if not token:
        # Fallback: read from local tfstate which we always have
        raw_data["source"] = "local_tfstate"
        raw_data["note"]   = "Live GCP API unavailable — showing Terraform state data."
        try:
            state_path = f"{TF_DIR}/terraform.tfstate"
            if os.path.exists(state_path):
                state = json.load(open(state_path))
                resources = state.get("resources", [])
                raw_data["resources"] = [
                    {
                        "type":   r.get("type"),
                        "name":   r.get("name"),
                        "values": {
                            k: v for k, v in
                            (r.get("instances") or [{}])[0].get("attributes", {}).items()
                            if k in ("name","machine_type","zone","status","self_link",
                                     "disk_size_gb","location","url","storage_class")
                        }
                    }
                    for r in resources
                    if not name or name.lower() in r.get("name","").lower()
                ]
        except Exception as exc:
            raw_data["tfstate_error"] = str(exc)

    else:
        try:
            # ── LIST VMs / VM INFO ────────────────────────────────────────
            if qtype in ("list_vms", "vm_info"):
                data = _gcp_get(
                    f"/compute/v1/projects/{project}/aggregated/instances",
                    token, {"maxResults": "50"})
                vms = []
                for zone_data in (data.get("items") or {}).values():
                    for v in (zone_data.get("instances") or []):
                        if name and name.lower() not in v.get("name","").lower():
                            continue
                        vms.append({
                            "name":         v.get("name"),
                            "zone":         v.get("zone","").split("/")[-1],
                            "machine_type": v.get("machineType","").split("/")[-1],
                            "status":       v.get("status"),
                            "internal_ip":  (v.get("networkInterfaces") or [{}])[0].get("networkIP"),
                            "external_ip":  ((v.get("networkInterfaces") or [{}])[0]
                                             .get("accessConfigs") or [{}])[0].get("natIP","none"),
                            "disk_gb":      sum(int(d.get("diskSizeGb",0))
                                               for d in v.get("disks",[])),
                            "os_image":     (v.get("disks") or [{}])[0].get("licenses",[""])[0].split("/")[-1],
                            "created":      v.get("creationTimestamp","")[:10],
                            "labels":       v.get("labels", {}),
                        })
                raw_data["vms"] = vms
                if data.get("error"):
                    raw_data["api_error"] = data["error"]

            # ── BUCKET LIST / INFO / SIZE ─────────────────────────────────
            elif qtype in ("list_buckets", "bucket_info", "bucket_size"):
                data = _gcp_get(
                    f"/storage/v1/b",
                    token, {"project": project, "maxResults": "50",
                            "fields": "items(name,location,storageClass,timeCreated,selfLink)"})
                buckets = [b for b in (data.get("items") or [])
                           if not name or name.lower() in b.get("name","").lower()]
                raw_data["buckets"] = [
                    {"name": b.get("name"), "location": b.get("location"),
                     "storage_class": b.get("storageClass"),
                     "created": b.get("timeCreated","")[:10]}
                    for b in buckets
                ]
                if qtype == "bucket_size" and raw_data["buckets"]:
                    # Get object stats for first 3 buckets
                    for b in raw_data["buckets"][:3]:
                        stats = _gcp_get(
                            f"/storage/v1/b/{b['name']}/o",
                            token, {"maxResults": "1000", "fields": "items(size)"})
                        items = stats.get("items") or []
                        total = sum(int(i.get("size", 0)) for i in items)
                        b["total_bytes"] = total
                        b["total_mb"]    = round(total / 1024 / 1024, 2)
                        b["object_count"] = len(items)

            # ── LOGS ──────────────────────────────────────────────────────
            elif qtype == "logs":
                import json as _json, urllib.request, urllib.parse
                from datetime import timezone
                end_dt   = datetime.utcnow().replace(tzinfo=timezone.utc)
                start_dt = datetime.utcfromtimestamp(
                    end_dt.timestamp() - minutes * 60).replace(tzinfo=timezone.utc)
                filter_str = log_filter or (
                    'resource.type=("gce_instance" OR "gcs_bucket" OR '
                    '"cloud_run_revision" OR "global")'
                )
                filter_str += (f' AND timestamp>="{start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}"'
                               f' AND timestamp<="{end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
                body = _json.dumps({
                    "resourceNames": [f"projects/{project}"],
                    "filter": filter_str,
                    "orderBy": "timestamp desc",
                    "pageSize": 20,
                }).encode()
                req = urllib.request.Request(
                    "https://logging.googleapis.com/v2/entries:list",
                    data=body,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=20)
                entries = _json.loads(resp.read()).get("entries", [])
                raw_data["log_entries"] = [
                    {
                        "time":     e.get("timestamp","")[:19],
                        "severity": e.get("severity","INFO"),
                        "resource": e.get("resource",{}).get("type",""),
                        "message":  (e.get("textPayload")
                                     or str(e.get("jsonPayload",{}).get("message",""))
                                     or str(e.get("protoPayload",{}).get("methodName","")))[:200],
                    } for e in entries[:15]
                ]
                raw_data["count"] = len(entries)

            # ── CLOUD RUN ─────────────────────────────────────────────────
            elif qtype == "run_services":
                # Cloud Run v2 API
                import urllib.request, json as _json
                url = (f"https://run.googleapis.com/v2/projects/{project}"
                       f"/locations/-/services")
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/json"})
                resp = urllib.request.urlopen(req, timeout=20)
                svcs = _json.loads(resp.read()).get("services", [])
                raw_data["services"] = [
                    {
                        "name":   s.get("name","").split("/")[-1],
                        "region": s.get("name","").split("/")[5] if "/" in s.get("name","") else "",
                        "url":    s.get("uri",""),
                        "state":  s.get("terminalCondition",{}).get("state",""),
                        "latest": s.get("latestReadyRevision","").split("/")[-1],
                    } for s in svcs
                ]

            # ── BILLING SUMMARY ───────────────────────────────────────────
            elif qtype == "billing":
                # Get project info + resource counts as billing proxy
                vms_d = _gcp_get(
                    f"/compute/v1/projects/{project}/aggregated/instances",
                    token, {"maxResults": "100"})
                vm_list = []
                for zd in (vms_d.get("items") or {}).values():
                    vm_list.extend(zd.get("instances") or [])
                bkt_d = _gcp_get(
                    f"/storage/v1/b", token,
                    {"project": project, "maxResults": "50",
                     "fields": "items(name,storageClass)"})
                raw_data["vm_count"]      = len(vm_list)
                raw_data["running_vms"]   = sum(1 for v in vm_list if v.get("status")=="RUNNING")
                raw_data["machine_types"] = list({v.get("machineType","").split("/")[-1]
                                                  for v in vm_list})
                raw_data["bucket_count"]  = len(bkt_d.get("items") or [])
                raw_data["note"] = ("Exact cost figures require Billing Export to BigQuery. "
                                    "Showing resource inventory for cost estimation.")

            # ── GENERIC / VM METRICS ──────────────────────────────────────
            else:
                # Aggregate: list VMs + buckets for a general overview
                vms_d = _gcp_get(
                    f"/compute/v1/projects/{project}/aggregated/instances",
                    token, {"maxResults": "50"})
                vms = []
                for zd in (vms_d.get("items") or {}).values():
                    for v in (zd.get("instances") or []):
                        vms.append({
                            "name":         v.get("name"),
                            "zone":         v.get("zone","").split("/")[-1],
                            "machine_type": v.get("machineType","").split("/")[-1],
                            "status":       v.get("status"),
                            "disk_gb":      sum(int(d.get("diskSizeGb",0))
                                               for d in v.get("disks",[])),
                        })
                raw_data["vms"] = vms
                bkt_d = _gcp_get(f"/storage/v1/b", token,
                                  {"project": project, "maxResults":"50",
                                   "fields":"items(name,location)"})
                raw_data["buckets"] = [{"name":b.get("name"),"location":b.get("location")}
                                        for b in (bkt_d.get("items") or [])]

        except Exception as exc:
            raw_data["exception"] = str(exc)
            _log(jid, f"⚠️ REST query error: {exc}")

    # ── Synthesise with Gemini ────────────────────────────────────────────
    _log(jid, f"🤖 Synthesising answer…")
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        synth = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            generation_config=genai.GenerationConfig(max_output_tokens=600, temperature=0.1),
        )
        synth_prompt = (
            f"You are an AI SRE for Google Cloud. "
            f"The user asked about: \"{qtype}\" "
            f"{'for resource: ' + name if name else ''}.\n\n"
            f"Live GCP data collected:\n{json.dumps(raw_data, indent=2)[:3000]}\n\n"
            f"Write a clear, concise answer. Use emojis for status icons. "
            f"Format VM lists as: 🖥️ name | type | zone | status | disk.\n"
            f"Format bucket lists as: 🪣 name | location | class | size.\n"
            f"If data came from tfstate note it. If error occurred, explain clearly."
        )
        resp = synth.generate_content(synth_prompt)
        return resp.text.strip()
    except Exception as exc:
        # Plain fallback render
        if raw_data.get("vms"):
            lines = [f"🖥️ **{v['name']}** | {v['machine_type']} | {v['zone']} | "
                     f"{v['status']} | 💾{v['disk_gb']}GB"
                     for v in raw_data["vms"]]
            return "\n".join(lines) or "No VMs found."
        if raw_data.get("buckets"):
            lines = [f"🪣 **{b['name']}** | {b.get('location','')} | {b.get('storage_class','')}"
                     for b in raw_data["buckets"]]
            return "\n".join(lines)
        if raw_data.get("log_entries"):
            lines = [f"[{e['time']}] `{e['severity']}` {e['message'][:100]}"
                     for e in raw_data["log_entries"]]
            return "\n".join(lines)
        return f"Raw data: {json.dumps(raw_data)[:400]}"

# ---------------------------------------------------------------------------
# Repo Deploy Engine
# Accepts a git repo URL + user prompt → analyses repo → generates Terraform
# HCL via Gemini → writes to main.tf → runs plan → returns result for apply.
#
# Supports two scenarios:
#   1. Chat/Voice: "deploy app from https://github.com/owner/repo"
#   2. UI panel:   user pastes URL + optional prompt → calls start_deploy_job()
#
# Flow:
#   clone repo → scan files → read sre.yaml (if exists) → Gemini analyses
#   → generates Cloud Run / GCE / GKE HCL → write_code() → run_plan()
# ---------------------------------------------------------------------------

def _clone_repo(repo_url: str, branch: str, token: str = "") -> str:
    """
    Clone a git repo to a temp directory. Returns the local path.
    Supports: public repos, GitHub token auth, GitLab token auth.
    token: GitHub PAT or GitLab deploy token (optional for public repos).
    """
    import tempfile, shutil

    # Inject token into URL if provided
    auth_url = repo_url
    if token:
        if "github.com" in repo_url:
            auth_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
        elif "gitlab.com" in repo_url:
            auth_url = repo_url.replace("https://", f"https://oauth2:{token}@")

    dest = tempfile.mkdtemp(prefix="sre_repo_")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch,
             "--single-branch", auth_url, dest],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            # Try without branch (some repos use 'master')
            result2 = subprocess.run(
                ["git", "clone", "--depth", "1", auth_url, dest + "_2"],
                capture_output=True, text=True, timeout=60
            )
            if result2.returncode != 0:
                shutil.rmtree(dest, ignore_errors=True)
                raise RuntimeError(
                    f"git clone failed: {result.stderr[:200] or result2.stderr[:200]}")
            shutil.rmtree(dest, ignore_errors=True)
            return dest + "_2"
        return dest
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _scan_repo(repo_dir: str) -> dict:
    """
    Scan repo directory and return a structured summary:
    - file tree (top 3 levels)
    - detected project type
    - sre.yaml contents if present
    - existing *.tf files
    - Dockerfile presence
    - key config files (package.json, requirements.txt, etc.)
    """
    import yaml as _yaml

    summary = {
        "has_dockerfile":   False,
        "has_tf_files":     False,
        "has_sre_yaml":     False,
        "has_docker_compose": False,
        "project_type":     "unknown",
        "languages":        [],
        "sre_config":       {},
        "tf_files":         [],
        "file_tree":        [],
        "key_files":        {},
    }

    lang_signals = {
        "requirements.txt": "python",
        "setup.py":         "python",
        "pyproject.toml":   "python",
        "package.json":     "nodejs",
        "go.mod":           "golang",
        "Cargo.toml":       "rust",
        "pom.xml":          "java",
        "build.gradle":     "java",
        "Gemfile":          "ruby",
    }

    for root, dirs, files in os.walk(repo_dir):
        # Skip hidden dirs and node_modules
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("node_modules", ".git", "__pycache__", "vendor")]
        rel = os.path.relpath(root, repo_dir)
        depth = rel.count(os.sep)
        if depth > 2:
            continue

        for f in files:
            fpath = os.path.join(root, f)
            rel_f = os.path.relpath(fpath, repo_dir)
            summary["file_tree"].append(rel_f)

            # Dockerfile
            if f in ("Dockerfile", "dockerfile"):
                summary["has_dockerfile"] = True
                summary["project_type"]   = "docker"

            # docker-compose
            if f in ("docker-compose.yml", "docker-compose.yaml"):
                summary["has_docker_compose"] = True

            # Terraform files
            if f.endswith(".tf"):
                summary["has_tf_files"] = True
                summary["tf_files"].append(rel_f)
                try:
                    summary["key_files"][rel_f] = open(fpath).read()[:1500]
                except Exception:
                    pass

            # sre.yaml
            if f in ("sre.yaml", "sre.yml"):
                summary["has_sre_yaml"] = True
                try:
                    raw = open(fpath).read()
                    try:
                        import yaml as _y
                        summary["sre_config"] = _y.safe_load(raw) or {}
                    except Exception:
                        summary["sre_config"] = {"_raw": raw[:500]}
                except Exception:
                    pass

            # Language signals
            if f in lang_signals and lang_signals[f] not in summary["languages"]:
                summary["languages"].append(lang_signals[f])
                try:
                    summary["key_files"][f] = open(fpath).read()[:800]
                except Exception:
                    pass

    # Infer project type if not set by Dockerfile
    if summary["project_type"] == "unknown" and summary["languages"]:
        summary["project_type"] = summary["languages"][0]

    return summary


def _gemini_analyse_repo(scan: dict, repo_url: str, app_name: str,
                          environment: str, user_prompt: str, jid: str) -> str:
    """
    Ask Gemini to analyse the repo scan and generate Terraform HCL
    for deploying this app to GCP. Returns raw HCL string.
    """
    _log(jid, "🤖 Gemini analysing repository…")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        generation_config=genai.GenerationConfig(
            max_output_tokens=3000,
            temperature=0.1,
        ),
    )

    region   = GCP_DEFAULT_REGION
    project  = GCP_PROJECT_ID
    sre_cfg  = scan.get("sre_config", {})
    env_cfg  = (sre_cfg.get("environments") or {}).get(environment, {})

    prompt = f"""You are an expert GCP infrastructure engineer.
Analyse this repository and generate Terraform HCL to deploy it to GCP.

Repository URL: {repo_url}
App name: {app_name}
Environment: {environment}
GCP Project: {project}
Region: {region}
User request: "{user_prompt or 'Deploy this application'}"

Repository scan:
- Project type: {scan['project_type']}
- Languages: {scan['languages']}
- Has Dockerfile: {scan['has_dockerfile']}
- Has existing .tf files: {scan['has_tf_files']}
- Has sre.yaml: {scan['has_sre_yaml']}
- sre.yaml config: {json.dumps(sre_cfg, indent=2)[:800]}
- Key files: {json.dumps({k: v[:400] for k, v in scan.get('key_files', {}).items()}, indent=2)[:2000]}

RULES:
- NEVER generate provider "google" blocks or terraform {{}} blocks
- Generate ONLY the resource/data blocks needed
- Use image = "gcr.io/{project}/{app_name}:latest" for Cloud Run (customer will push their image)
- If Dockerfile exists → deploy as google_cloud_run_v2_service
- If has_tf_files but no Dockerfile → use existing tf structure, supplement missing resources
- If static site (index.html, no backend) → deploy as google_storage_bucket with website config
- If sre.yaml exists → follow its configuration exactly
- Add google_cloud_run_v2_service_iam_member for allUsers if environment=production (public access)
- Memory: {env_cfg.get('memory', '512Mi')}, CPU: {env_cfg.get('cpu', '1')}
- Min instances: {env_cfg.get('min_instances', 0)}, Max: {env_cfg.get('max_instances', 3)}

Return ONLY the Terraform HCL resource blocks. No explanation, no markdown fences."""

    resp = model.generate_content(prompt)
    raw  = (resp.text or "").strip()
    raw  = re.sub(r'```(?:hcl|terraform)?\n?', '', raw)
    raw  = re.sub(r'```', '', raw).strip()
    return raw


def _run_repo_deploy(repo_url: str, branch: str = "main", app_name: str = "",
                     environment: str = "production", user_prompt: str = "",
                     jid: str = "", user: str = "unknown",
                     repo_token: str = "") -> dict:
    """
    Full repo deploy pipeline:
    1. Clone repo
    2. Scan files
    3. Gemini generates HCL
    4. write_code() → main.tf
    5. run_plan()
    Returns dict with plan, meta, message, or error key.
    """
    import shutil

    repo_dir = None
    try:
        # ── Derive app name from repo URL if not given ─────────────────
        if not app_name:
            app_name = repo_url.rstrip("/").split("/")[-1]
            app_name = re.sub(r'\.git$', '', app_name)
            app_name = re.sub(r'[^a-z0-9\-]', '-', app_name.lower())[:30]

        _log(jid, f"📦 Cloning {repo_url} (branch: {branch})…")

        # ── Get optional token from env ─────────────────────────────────
        if not repo_token:
            repo_token = os.environ.get("GITHUB_TOKEN", "")

        # ── Clone ───────────────────────────────────────────────────────
        try:
            repo_dir = _clone_repo(repo_url, branch, repo_token)
            _log(jid, f"✅ Cloned to {repo_dir}")
        except Exception as exc:
            return {"error": f"❌ Could not clone repository: {exc}\n"
                             f"Make sure the URL is correct and the repo is public "
                             f"(or add GITHUB_TOKEN to your .env for private repos)."}

        # ── Scan repo ───────────────────────────────────────────────────
        _log(jid, "🔍 Scanning repository structure…")
        scan = _scan_repo(repo_dir)
        _log(jid, f"   Type: {scan['project_type']} | "
                  f"Dockerfile: {scan['has_dockerfile']} | "
                  f"Terraform: {scan['has_tf_files']} | "
                  f"sre.yaml: {scan['has_sre_yaml']}")
        _log(jid, f"   Files found: {len(scan['file_tree'])}")

        # ── If repo has its own .tf files, offer to use them ───────────
        if scan["has_tf_files"] and not scan["has_dockerfile"]:
            _log(jid, "📋 Repo has existing Terraform files — merging with managed state…")

        # ── Snapshot before writing ─────────────────────────────────────
        TerraformTools.snapshot_main_tf()

        # ── Gemini generates HCL ────────────────────────────────────────
        hcl = _gemini_analyse_repo(
            scan, repo_url, app_name, environment, user_prompt, jid)

        if not hcl or len(hcl.strip()) < 20:
            return {"error": "❌ Gemini could not generate deployment HCL for this repository. "
                             "Make sure the repo has a Dockerfile or recognisable project structure."}

        _log(jid, f"📝 Generated {len(hcl.split(chr(10)))} lines of Terraform HCL")

        # ── Strip forbidden blocks + write to main.tf ───────────────────
        hcl_clean = TerraformTools._strip_provider_blocks(hcl)
        result    = TerraformTools.write_code(hcl_clean)

        if result.startswith("DUPLICATE:"):
            dupes = result.replace("DUPLICATE:", "")
            _log(jid, f"⚠️ Duplicate resources detected: {dupes} — updating in place")
            # For redeploy: overwrite the duplicate blocks
            existing = TerraformTools.read_code()
            for d in dupes.split(","):
                d = d.strip()
                if d:
                    existing = re.sub(
                        rf'(resource\s+"[\w"]+"\s+"{re.escape(d)}"\s+\{{[^}}]*(?:\{{[^}}]*\}}[^}}]*)*\}})',
                        '', existing, flags=re.DOTALL)
            with open(f"{TF_DIR}/main.tf", "w") as f:
                f.write(existing.strip() + "\n\n" + hcl_clean.strip() + "\n")

        # ── Run plan ────────────────────────────────────────────────────
        _log(jid, "🔎 Running terraform plan…")
        plan = TerraformTools.run_plan(jid)

        if plan.startswith("❌"):
            TerraformTools.revert_to_snapshot()
            return {"error": f"❌ Terraform plan failed:\n{plan}"}

        # ── Build deploy meta ───────────────────────────────────────────
        meta = {
            "repo_url":      repo_url,
            "branch":        branch,
            "app_name":      app_name,
            "environment":   environment,
            "project_type":  scan["project_type"],
            "has_dockerfile": scan["has_dockerfile"],
            "has_sre_yaml":  scan["has_sre_yaml"],
            "image_hint":    f"gcr.io/{GCP_PROJECT_ID}/{app_name}:latest",
        }

        deploy_type = scan["project_type"] or "application"
        image_note = ""
        if scan["has_dockerfile"]:
            image_note = (f"\n\n💡 **Before applying:** push your Docker image:\n"
                          f"```\ngcloud builds submit --tag gcr.io/{GCP_PROJECT_ID}/{app_name}:latest .\n```")

        msg = (f"🚀 **Deployment plan ready** for `{app_name}` ({deploy_type}) "
               f"from `{repo_url.split('/')[-1]}`\n"
               f"Environment: **{environment}** · Branch: **{branch}**"
               + image_note)

        return {"plan": plan, "meta": meta, "message": msg}

    finally:
        # Always clean up temp clone
        if repo_dir:
            try:
                import shutil as _sh
                _sh.rmtree(repo_dir, ignore_errors=True)
            except Exception:
                pass


def start_deploy_job(repo_url: str, branch: str = "main", app_name: str = "",
                     environment: str = "production", user_prompt: str = "",
                     repo_token: str = "", user: str = "unknown") -> str:
    """
    Start an async repo deploy job. Returns job ID.
    Called by:
      1. _agent_worker when action="deploy" (chat/voice path)
      2. UI deploy panel directly (URL input path)
    """
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "deploy"
        JOBS[jid]["_user"]     = user

    def _worker():
        result = _run_repo_deploy(
            repo_url    = repo_url,
            branch      = branch,
            app_name    = app_name,
            environment = environment,
            user_prompt = user_prompt,
            jid         = jid,
            user        = user,
            repo_token  = repo_token,
        )
        if result.get("error"):
            _error(jid, result["error"])
            return

        plan         = result["plan"]
        current_code = TerraformTools.read_code()
        plan_details = parse_plan_details(plan, current_code)
        cost_est     = estimate_plan_cost(plan_details, current_code, jid)
        security     = audit_security(current_code, plan_details, jid)

        _finish(jid, {
            "status":         "success",
            "plan":           plan,
            "plan_details":   plan_details,
            "cost_estimate":  cost_est,
            "security_audit": security,
            "auto_fixed":     [],
            "is_destroy":     False,
            "deploy_meta":    result.get("meta", {}),
            "message":        result.get("message", "Deployment plan ready."),
            "collateral_warning": [],
        })

    threading.Thread(target=_worker, daemon=True).start()
    return jid


def start_agent_job(messages: list, user: str = "unknown") -> str:
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "plan"
        JOBS[jid]["_user"]     = user
    threading.Thread(target=_agent_worker, args=(jid, messages), daemon=True).start()
    return jid


# ---------------------------------------------------------------------------
# Drift Detection Engine
# Compares terraform state against live GCP resources via REST API.
# Detects: deleted resources (ghost state), modified resources (config drift).
# ---------------------------------------------------------------------------

def detect_drift(jid: str) -> dict:
    """
    Run terraform plan -refresh-only to detect drift.
    Works with both local and GCS remote state.
    Runs terraform init first to pull remote state from GCS if configured.
    Returns: {clean, deleted, modified, summary, raw_plan, how_to_fix}
    """
    _log(jid, "🔄 Checking infrastructure drift (terraform plan -refresh-only)…")
    _log(jid, "   Reads live GCP state — no changes will be made.")

    drift_report = {
        "clean":      False,
        "deleted":    [],
        "modified":   [],
        "summary":    "",
        "how_to_fix": "",
        "raw_plan":   "",
    }

    # ── Step 1: Init to connect to GCS remote state ──────────────────────
    _log(jid, "🔧 Initializing Terraform (connecting to GCS remote state)…")
    init_result = TerraformTools.run_init(jid)
    if init_result not in ("ok", "") and "✅" not in init_result:
        _log(jid, f"⚠️ Init warning: {init_result[:120]} — attempting anyway…")

    # ── Step 2: Check what's in remote state ─────────────────────────────
    state_list_res = subprocess.run(
        ["terraform", "state", "list"],
        cwd=TF_DIR, capture_output=True, text=True, timeout=60
    )
    state_list_out = state_list_res.stdout.strip()

    if state_list_res.returncode != 0 or not state_list_out:
        # No resources in state at all
        drift_report["summary"]    = "⚠️ No resources in Terraform state — nothing has been applied yet."
        drift_report["how_to_fix"] = "Deploy some infrastructure first (e.g. 'create vm test e2-micro us-central1' then Apply), then check drift."
        _log(jid, drift_report["summary"])
        return drift_report

    resource_count = len([l for l in state_list_out.splitlines() if l.strip()])
    _log(jid, f"📋 {resource_count} resource(s) in state — running refresh-only plan…")

    # ── Step 3: Restore main.tf from GCS if missing locally ──────────────
    # On Cloud Run the container is ephemeral — main.tf is gone after restart.
    # terraform plan -refresh-only still needs the .tf config files.
    main_tf_path = f"{TF_DIR}/main.tf"
    if not os.path.exists(main_tf_path) or not open(main_tf_path).read().strip():
        state_bucket = os.environ.get("TF_STATE_BUCKET", "")
        if state_bucket:
            _log(jid, "📥 Restoring main.tf from GCS…")
            try:
                from google.cloud import storage as _gcs
                _client = _gcs.Client()
                _blob   = _client.bucket(state_bucket).blob("main.tf")
                if _blob.exists():
                    _blob.download_to_filename(main_tf_path)
                    _log(jid, "✅ main.tf restored from GCS")
                else:
                    # main.tf not in GCS either — create a minimal placeholder
                    # that lets terraform plan read state without failing on missing config
                    _log(jid, "⚠️ main.tf not in GCS — generating from state list…")
                    _placeholder = "# Restored for drift check\n"
                    for addr in state_list_out.splitlines():
                        addr = addr.strip()
                        if "." in addr:
                            rtype, rname = addr.split(".", 1)
                            _placeholder += (
                                f'\nresource "{rtype}" "{rname}" {{}}\n'
                            )
                    with open(main_tf_path, "w") as _f:
                        _f.write(_placeholder)
            except Exception as _exc:
                _log(jid, f"⚠️ Could not restore main.tf: {_exc} — drift may be incomplete")

    # ── Step 4: Run the actual drift check ────────────────────────────────
    res = subprocess.run(
        ["terraform", "plan", "-refresh-only", "-no-color"],
        cwd=TF_DIR, capture_output=True, text=True, timeout=180
    )
    output = res.stdout + res.stderr
    drift_report["raw_plan"] = output[:4000]

    # ── Clean: no drift ───────────────────────────────────────────────────
    clean_signals = [
        "No changes",
        "Your infrastructure matches the configuration",
        "state is up-to-date",
    ]
    if any(s.lower() in output.lower() for s in clean_signals):
        drift_report["clean"]      = True
        drift_report["summary"]    = f"✅ No drift — all {resource_count} resource(s) match GCP exactly."
        drift_report["how_to_fix"] = ""
        _log(jid, drift_report["summary"])
        return drift_report

    # ── Parse deleted and modified resources ──────────────────────────────
    seen_deleted  = set()
    seen_modified = set()

    for line in output.splitlines():
        clean_line = line.strip().lstrip("│ ").strip()

        # Deleted: "google_compute_instance.vm-prod has been deleted"
        m_del = re.search(
            r'(google_[\w]+\.[\w\-]+)\s+(?:has been deleted|no longer exists|was deleted)',
            clean_line, re.IGNORECASE)
        if not m_del:
            m_del = re.search(r'(google_[\w]+\.[\w\-]+)\s+must be replaced', clean_line)
        if m_del:
            addr = m_del.group(1).strip()
            if addr not in seen_deleted:
                seen_deleted.add(addr)
                parts = addr.split(".", 1)
                drift_report["deleted"].append({
                    "address":       addr,
                    "resource_type": parts[0],
                    "resource_name": parts[1] if len(parts) > 1 else addr,
                    "detail":        clean_line[:120],
                })

        # Modified: "~ google_compute_instance.vm-prod"
        m_mod = re.match(r'[~]\s+(google_[\w]+\.[\w\-]+)', clean_line)
        if m_mod:
            addr = m_mod.group(1).strip()
            if addr not in seen_modified and addr not in seen_deleted:
                seen_modified.add(addr)
                drift_report["modified"].append({
                    "address": addr,
                    "detail":  "Modified outside Terraform (e.g. via GCP Console)",
                })

    n_del = len(drift_report["deleted"])
    n_mod = len(drift_report["modified"])

    # ── Plan errored with no parsed resources ─────────────────────────────
    if n_del == 0 and n_mod == 0 and res.returncode != 0:
        drift_report["summary"] = (
            "⚠️ Terraform refresh failed — check credentials or state file.\n"
            f"Error:\n{output[-400:]}"
        )
        drift_report["how_to_fix"] = (
            "1. Verify GOOGLE_CREDENTIALS is set correctly\n"
            "2. Check TF_STATE_BUCKET is accessible\n"
            "3. Ask agent: 'restore previous version' if state is corrupted"
        )
        _log(jid, "❌ Plan error during drift check")
        return drift_report

    # ── Build human summary ───────────────────────────────────────────────
    parts = []
    if n_del:
        names = ", ".join(f"`{d['address']}`" for d in drift_report["deleted"][:3])
        parts.append(f"🗑️ **{n_del} resource(s) deleted from GCP** (still in Terraform state):\n  {names}")
    if n_mod:
        names = ", ".join(f"`{m['address']}`" for m in drift_report["modified"][:3])
        parts.append(f"✏️ **{n_mod} resource(s) modified** via GCP Console:\n  {names}")

    drift_report["summary"] = "\n\n".join(parts) if parts else "⚠️ Drift detected — check plan output below."
    drift_report["how_to_fix"] = (
        "**Option A — Accept GCP changes** (update Terraform state to match GCP reality):\n"
        "  Click 'Reconcile state' in the Drift Detection panel\n\n"
        "**Option B — Revert to Terraform code** (push your code config back to GCP):\n"
        "  Click 'Revert to code' in the Drift Detection panel"
    )

    _log(jid, f"⚠️ Drift found: {n_del} deleted, {n_mod} modified")
    return drift_report


def _fix_drift(drift: dict, action: str, jid: str) -> str:
    """
    Fix drift based on chosen action.
    action: "remove_state"  — remove deleted resources from state (accept the deletion)
            "reconcile"     — terraform apply -refresh-only (sync state to GCP reality)
    """
    if action == "remove_state":
        results = []
        for r in drift.get("deleted", []):
            addr = r["address"]
            _log(jid, f"🗑️ Removing {addr} from state…")
            res = subprocess.run(
                ["terraform", "state", "rm", addr],
                cwd=TF_DIR, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                results.append(f"✅ Removed {addr} from state")
            else:
                results.append(f"❌ Failed to remove {addr}: {res.stderr[:100]}")
        return "\n".join(results) or "Nothing to remove."

    elif action == "reconcile":
        _log(jid, "🔄 Applying refresh-only to sync state…")
        res = subprocess.run(
            ["terraform", "apply", "-refresh-only", "-auto-approve", "-no-color"],
            cwd=TF_DIR, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            return "✅ State reconciled — Terraform now reflects GCP reality."
        return f"❌ Reconcile failed:\n{res.stderr[:300]}"

    return "Unknown action."


def start_drift_job(user: str = "unknown") -> str:
    """Start an async drift detection job. Returns job ID."""
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "drift"
        JOBS[jid]["_user"]     = user

    def _worker():
        try:
            init = TerraformTools.run_init(jid)
            if init != "ok":
                _error(jid, f"❌ Init failed: {init}")
                return
            drift = detect_drift(jid)
            _finish(jid, {"status": "success", "drift": drift,
                          "message": drift["summary"]})
        except Exception as exc:
            _error(jid, f"❌ Drift detection failed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()
    return jid


def start_drift_fix_job(drift: dict, action: str, user: str = "unknown") -> str:
    """Start an async drift fix job."""
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "drift_fix"
        JOBS[jid]["_user"]     = user

    def _worker():
        try:
            result = _fix_drift(drift, action, jid)
            _finish(jid, {"status": "success", "message": result})
        except Exception as exc:
            _error(jid, f"❌ Drift fix failed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()
    return jid


def start_voice_job(audio_bytes: bytes, mime_type: str = "audio/wav",
                    model: str = None,
                    user: str = "unknown") -> str:
    """
    Transcribe voice command via Gemini audio understanding.
    model: gemini-2.5-flash
    """
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "voice"
        JOBS[jid]["_user"]     = user
    _voice_model = (model or VOICE_MODEL).strip()

    def _worker():
        prompt = (
            "You are an AI SRE agent for Google Cloud Platform. "
            "The user has spoken a voice command to manage GCP infrastructure via Terraform.\n\n"
            "Listen carefully and respond with ONLY a JSON object - no markdown, no explanation:\n"
            "{\n"
            '  "transcription": "<exact words spoken>",\n'
            '  "command": "<clean imperative command - see rules below>",\n'
            '  "spoken_response": "<one sentence confirming what you understood>"\n'
            "}\n\n"
            "## COMMAND EXTRACTION RULES\n"
            "Preserve ALL details: machine type, region, OS, disk size, labels.\n\n"
            "CRITICAL - name vs machine type:\n"
            "- Machine types: e2-micro, e2-small, e2-medium, e2-standard-*, n1-*, n2-*, t2d-*, c2-*\n"
            '- Words "micro"/"small"/"medium" in speech = machine type (e2-micro/e2-small/e2-medium)\n'
            "- If user gives no explicit resource name, auto-generate: type+region e.g. vm-micro-usc1\n\n"
            "OS image mapping (add as os=<value> in command):\n"
            '  ubuntu 24 / ubuntu 24.04  -> os=ubuntu-2404-lts\n'
            '  ubuntu 22 / ubuntu 22.04  -> os=ubuntu-2204-lts\n'
            '  ubuntu 20                 -> os=ubuntu-2004-lts\n'
            '  debian                    -> os=debian-12\n'
            '  centos / rocky            -> os=rocky-linux-9\n'
            '  windows                   -> os=windows-2022\n'
            '  cos / container optimized -> os=cos-stable\n\n'
            "Region normalisation (fix spoken regions to GCP format):\n"
            '  us central 1 / us-central-1 -> us-central1\n'
            '  europe west 1               -> europe-west1\n'
            '  asia southeast 1            -> asia-southeast1\n\n'
            "Command format examples:\n"
            '  "create vm type micro in us-central1 with ubuntu 24"\n'
            '  -> create vm vm-micro-usc1 e2-micro us-central1 os=ubuntu-2404-lts\n\n'
            '  "create vm web-server small europe-west1 ubuntu 22"\n'
            '  -> create vm web-server e2-small europe-west1 os=ubuntu-2204-lts\n\n'
            '  "remove bucket logs-bucket"\n'
            '  -> destroy bucket logs-bucket\n\n'

            "## OBSERVABILITY & QUERY COMMANDS\n"
            "Use action=query for ANY question about existing resources, state, metrics or logs.\n"
            "Examples:\n"
            '  "show me all VMs" / "list instances"\n'
            '  -> query list_vms\n\n'
            '  "what is the disk size / storage of vm X"\n'
            '  -> query vm_info resource=X\n\n'
            '  "show CPU usage of vm X last 30 minutes"\n'
            '  -> query vm_metrics resource=X metric=cpu minutes=30\n\n'
            '  "show last API transactions / recent logs"\n'
            '  -> query logs minutes=60\n\n'
            '  "list all buckets" / "show storage buckets"\n'
            '  -> query list_buckets\n\n'
            '  "how big is bucket X" / "size of X"\n'
            '  -> query bucket_size resource=X\n\n'
            '  "show Cloud Run services"\n'
            '  -> query run_services\n\n'
            '  "what is my billing / cost this month"\n'
            '  -> query billing\n\n'
            "IMPORTANT: For query commands, set command = \"query <type> [resource=<name>] [metric=<m>] [minutes=<n>]\"\n"
            "If the audio is silent or unintelligible, set transcription and command to empty strings."
        )

        # Python SDK inline_data takes raw bytes (not base64 — that's REST API only)
        def make_audio_part(raw: bytes, mt: str) -> dict:
            return {"mime_type": mt, "data": raw}

        def extract_voice_data(raw_text: str) -> dict:
            """
            Robustly parse voice JSON response.
            Handles: complete JSON, truncated JSON, plain text, nested JSON-in-string.
            """
            cleaned = re.sub(r'```json|```', '', raw_text).strip()

            # 1. Try direct JSON parse
            try:
                d = json.loads(cleaned)
                if isinstance(d, dict) and d.get("transcription"):
                    # Guard: transcription field itself must not look like JSON
                    t = d["transcription"]
                    if t.startswith("{") or t.startswith("["):
                        # Model put JSON inside transcription — extract from it
                        try:
                            inner = json.loads(t)
                            d["transcription"] = inner.get("transcription", t)
                            d["command"]       = inner.get("command", d.get("command", t))
                        except Exception:
                            pass
                    return d
            except Exception:
                pass

            # 2. Truncated JSON — extract fields with regex
            def pull(field: str) -> str:
                m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned)
                return m.group(1).strip() if m else ""

            transcript = pull("transcription")
            command    = pull("command") or transcript
            spoken     = pull("spoken_response")

            if transcript:
                return {
                    "transcription":   transcript,
                    "command":         command,
                    "spoken_response": spoken or f"Got it — {command}",
                }

            # 3. Plain text fallback — treat entire response as the command
            if cleaned and not cleaned.startswith("{"):
                return {
                    "transcription":   cleaned,
                    "command":         cleaned,
                    "spoken_response": f"I heard: {cleaned}",
                }

            return {}

        fallback_chain = [_voice_model] + [
            m for m in ["gemini-2.5-flash"]
            if m != _voice_model
        ]
        errors = []

        for attempt_model in fallback_chain:
            try:
                genai.configure(api_key=GEMINI_API_KEY)
                _log(jid, f"🎙️ Transcribing with {attempt_model}… ({len(audio_bytes):,} bytes)")

                client = genai.GenerativeModel(
                    model_name=attempt_model,
                    generation_config=genai.GenerationConfig(
                        max_output_tokens=512,   # raised from 256 — prevents truncation
                        temperature=0.0,
                    ),
                )

                response = client.generate_content(
                    [make_audio_part(audio_bytes, mime_type), prompt]
                )
                raw_text = (response.text or "").strip()
                _log(jid, f"🔍 Raw response ({len(raw_text)} chars): {raw_text[:200]}")

                data = extract_voice_data(raw_text)

                transcript = (data.get("transcription") or "").strip()
                command    = (data.get("command") or transcript).strip()

                if not transcript:
                    _log(jid, f"⚠️ Empty transcription — raw: {raw_text[:100]}")
                    errors.append(f"{attempt_model}: empty transcription")
                    continue   # try next model in chain

                _log(jid, f"📝 {transcript[:120]}")
                _finish(jid, {
                    "status":          "success",
                    "transcription":   transcript,
                    "command":         command,
                    "spoken_response": data.get("spoken_response", f"Got it — {command}"),
                    "model_used":      attempt_model,
                })
                return

            except Exception as exc:
                full_err = repr(exc)
                errors.append(f"{attempt_model}: {full_err}")
                _log(jid, f"⚠️ {attempt_model} failed: {full_err}")

        _error(jid, "❌ Voice transcription failed:\n" + "\n".join(errors))

    threading.Thread(target=_worker, daemon=True).start()
    return jid


def start_apply_job(is_destroy: bool = False, destroy_target: dict = None,
                    apply_targets: list = None, user: str = "unknown") -> str:
    jid = _new_job()
    with JOBS_LOCK:
        JOBS[jid]["_job_type"] = "destroy" if is_destroy else ("apply_target" if apply_targets else "apply")
        JOBS[jid]["_user"]     = user
    threading.Thread(target=_apply_worker,
                     args=(jid, is_destroy, destroy_target, apply_targets),
                     daemon=True).start()
    return jid

# ---------------------------------------------------------------------------
# A7 UI Navigator — public API
# These publish requests to Redis which A7 handles asynchronously.
# Results come back via verify:result / navigate:result / qa:result channels.
# ---------------------------------------------------------------------------

def a7_navigate(goal: str, url: str = "https://console.cloud.google.com",
                user: str = "unknown") -> str:
    """
    Trigger A7 to navigate to a URL and accomplish a goal using Gemini Vision.
    Returns a session_id to poll for results.
    """
    import time as _t
    session_id = f"nav_{int(_t.time()*1000)}"
    _redis_publish("ui_navigate:request", {
        "goal":       goal,
        "url":        url,
        "session_id": session_id,
        "user":       user,
        "ts":         datetime.utcnow().isoformat() + "Z",
    })
    return session_id


def a7_visual_qa(url: str, checks: list[str] = None, user: str = "unknown") -> str:
    """
    Trigger A7 to visually QA a URL and return health status + screenshot.
    Returns a session_id.
    """
    import time as _t
    session_id = f"qa_{int(_t.time()*1000)}"
    _redis_publish("ui_qa:request", {
        "url":        url,
        "checks":     checks or ["Is the page loading correctly?",
                                  "Are there any errors or broken elements?",
                                  "Does this look like a healthy, functional application?"],
        "session_id": session_id,
        "user":       user,
        "ts":         datetime.utcnow().isoformat() + "Z",
    })
    return session_id


def a7_monitor_add(name: str, url: str, checks: list[str] = None) -> None:
    """Register a URL for periodic visual monitoring by A7."""
    _redis_publish("ui_monitor:start", {
        "name":   name,
        "url":    url,
        "checks": checks or ["Is the page healthy and working correctly?"],
        "ts":     datetime.utcnow().isoformat() + "Z",
    })


def a7_monitor_remove(name: str) -> None:
    """Stop monitoring a previously registered URL."""
    _redis_publish("ui_monitor:stop", {"name": name})


def get_a7_result(session_id: str) -> dict | None:
    """
    Poll Redis for a navigate/qa result by session_id.
    Returns the result dict or None if not yet available.
    Uses a short-lived key written by A7 into Redis.
    """
    rc = _get_redis()
    if not rc:
        return None
    try:
        raw = rc.get(f"a7:result:{session_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def a7_is_available() -> bool:
    """Check if A7 is reachable via Redis."""
    rc = _get_redis()
    return rc is not None
