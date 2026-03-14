import re
import time
import json
import os
import streamlit as st
from agent import (
    start_agent_job, start_apply_job, get_job,
    TerraformTools, get_pending_resources, get_created_resources,
    TF_DIR, get_existing_provider_aliases, extract_region_from_prompt,
    GCP_PROJECT_ID, GCP_DEFAULT_REGION, GEMINI_MODEL, VOICE_MODEL,
    parse_plan_details, read_tfstate, get_state_lock_info,
    force_unlock_state, start_state_push_job,
    estimate_plan_cost, audit_security, auto_fix_security,
    read_audit_log, get_version_history, start_rollback_job,
    start_voice_job,
)

st.set_page_config(page_title="AI SRE Agent — GCP", layout="wide", page_icon="☁️",
                   initial_sidebar_state="collapsed")

def get_current_user() -> str:
    """Read the authenticated username injected by the auth proxy header."""
    try:
        headers = st.context.headers
        return headers.get("X-Authenticated-User", "unknown")
    except Exception:
        return "unknown"

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

* { font-family: 'Inter', sans-serif; box-sizing: border-box; }
.stApp { background: #0d1117; }
.block-container { padding: 0 !important; max-width: 100% !important; }
section[data-testid="stSidebar"] { display: none; }
header[data-testid="stHeader"]   { display: none; }
#MainMenu, footer                { display: none; }
.stTextArea label, .stTextInput label { display: none; }

::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }

.msg-row-user      { display:flex; justify-content:flex-end;  margin:6px 16px; }
.msg-row-assistant { display:flex; justify-content:flex-start; margin:6px 16px; }

.bubble-user {
    background: #1565c0;
    color: #fff;
    border-radius: 16px 16px 4px 16px;
    padding: 9px 14px;
    max-width: 72%;
    font-size: 0.875rem;
    line-height: 1.55;
    word-break: break-word;
}
.bubble-assistant {
    background: #161b22;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 16px 16px 16px 4px;
    padding: 9px 14px;
    max-width: 82%;
    font-size: 0.875rem;
    line-height: 1.55;
    word-break: break-word;
}
.bubble-warning { background:#2d1f00; border-color:#d29922 !important; color:#f0c84a; }
.bubble-error   { background:#2d0f0f; border-color:#f85149 !important; color:#ff7b72; }
.bubble-success { background:#0d2818; border-color:#2ea043 !important; color:#56d364; }

.thinking-dot {
    display:inline-block; width:6px; height:6px;
    background:#8b949e; border-radius:50%; margin:0 2px;
    animation: blink 1.2s infinite;
}
.thinking-dot:nth-child(2) { animation-delay:0.2s; }
.thinking-dot:nth-child(3) { animation-delay:0.4s; }
@keyframes blink { 0%,80%,100%{opacity:0.2} 40%{opacity:1} }

.log-box {
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 10px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: #8b949e;
    max-height: 220px;
    overflow-y: auto;
    margin-top: 6px;
    white-space: pre-wrap;
    word-break: break-all;
}
.log-line-ok   { color: #3fb950; }
.log-line-err  { color: #f85149; }
.log-line-warn { color: #d29922; }
.log-line-info { color: #79c0ff; }

.res-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 9px 13px;
    margin: 5px 0;
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 0.8rem;
}
.res-card.pending { border-color: #9e6a03; }
.res-card.running { border-color: #2ea043; }
.res-name { color: #e6edf3; font-family:'JetBrains Mono'; font-weight:500; flex:1; }
.res-type { color: #484f58; font-family:'JetBrains Mono'; font-size:0.7rem; }

.badge {
    display:inline-block; padding:2px 10px; border-radius:20px;
    font-size:0.68rem; font-weight:600; letter-spacing:0.03em;
    font-family:'JetBrains Mono';
}
.badge-green  { background:#0d2818; color:#3fb950; border:1px solid #2ea043; }
.badge-yellow { background:#2d1f00; color:#d29922; border:1px solid #9e6a03; }
.badge-red    { background:#2d0f0f; color:#f85149; border:1px solid #f85149; }

.stButton > button {
    background: #21262d; color: #e6edf3;
    border: 1px solid #30363d; border-radius: 8px;
    font-size: 0.82rem; font-weight: 500;
    padding: 7px 16px; transition: all 0.15s; width: 100%;
}
.stButton > button:hover  { background: #30363d; border-color: #8b949e; }
.stButton > button:disabled { opacity: 0.25; cursor:not-allowed; }
.btn-apply   > button { background:#1a4731 !important; border-color:#2ea043 !important; color:#3fb950 !important; }
.btn-apply   > button:hover { background:#238636 !important; }
.btn-destroy > button { background:#3d0f0f !important; border-color:#f85149 !important; color:#f85149 !important; }
.btn-destroy > button:hover { background:#8b1a1a !important; }
.btn-send    > button { background:#1565c0 !important; border-color:#1976d2 !important; color:#fff !important; height:62px; }
.btn-send    > button:hover { background:#1976d2 !important; }

.stTextArea textarea {
    background: #161b22 !important; color: #e6edf3 !important;
    border: 1px solid #30363d !important; border-radius: 10px !important;
    font-size: 0.875rem !important; resize: none !important;
    line-height: 1.5 !important;
}
.stTextArea textarea:focus { border-color:#1565c0 !important; box-shadow:0 0 0 3px rgba(21,101,192,0.12) !important; }
.stTextArea textarea::placeholder { color:#484f58 !important; }

[data-testid="metric-container"] {
    background:#161b22; border:1px solid #30363d;
    border-radius:8px; padding:10px 14px;
}
[data-testid="metric-container"] label { color:#8b949e !important; font-size:0.7rem !important; text-transform:uppercase; letter-spacing:0.05em; }
[data-testid="stMetricValue"] { color:#e6edf3 !important; font-size:1.6rem !important; font-weight:700 !important; }

.streamlit-expanderHeader {
    background:#161b22 !important; border:1px solid #21262d !important;
    border-radius:8px !important; color:#8b949e !important; font-size:0.78rem !important;
}
details[open] .streamlit-expanderHeader { border-radius:8px 8px 0 0 !important; }

hr { border-color:#21262d !important; margin:0.6rem 0 !important; }
.stSuccess { background:#0d2818 !important; border-left:3px solid #2ea043 !important; }
.stWarning { background:#2d1f00 !important; border-left:3px solid #d29922 !important; }
.stError   { background:#2d0f0f !important; border-left:3px solid #f85149 !important; }
.stInfo    { background:#0c1e38 !important; border-left:3px solid #1565c0 !important; }

/* ── Remove Streamlit default padding so layout fills viewport ── */
section.main > div.block-container {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    max-width: 100% !important;
}

/* ── Make main content fill remaining height after header bars ── */
section.main { height: 100vh; overflow: hidden; }

/* ── Input dock: always at bottom of chat column ── */
.input-dock {
    position: relative;
    bottom: 0;
    background: #0d1117;
    padding: 8px 0 4px 0;
    z-index: 200;
    border-top: 1px solid #21262d;
}

/* ── Action buttons row ── */
.action-bar {
    padding: 6px 0 2px 0;
    background: #0d1117;
    border-top: 1px solid #21262d;
}

/* ── Live log box in right panel ── */
.log-box-live {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 8px 14px;
    font-family: "JetBrains Mono", monospace;
    font-size: 0.70rem;
    color: #8b949e;
    height: 260px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
    scroll-behavior: smooth;
}

/* ── Log label ── */
.log-label {
    font-family: "JetBrains Mono", monospace;
    font-size: 0.66rem;
    color: #484f58;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 3px;
}

/* ── Auto-scroll live log to bottom ── */
.log-box-live { overflow-anchor: none; }

/* ── Column gap tight ── */
[data-testid="column"] > div { gap: 0 !important; }
[data-testid="column"] { padding: 0 4px !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def colorize_logs(logs: list) -> str:
    html_lines = []
    for line in logs:
        lo = line.lower()
        if any(x in lo for x in ["✅", "success", "complete", "initialized", "applied"]):
            cls = "log-line-ok"
        elif any(x in lo for x in ["❌", "error", "failed", "exception"]):
            cls = "log-line-err"
        elif any(x in lo for x in ["⚠️", "warn", "destroy", "💣"]):
            cls = "log-line-warn"
        elif any(x in lo for x in ["🤖", "📨", "🔄", "📊", "🔍", "🔧", "🚀", "📝"]):
            cls = "log-line-info"
        else:
            cls = ""
        escaped = line.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        html_lines.append(f'<span class="{cls}">{escaped}</span>' if cls else escaped)
    return "\n".join(html_lines)

def region_flag(region: str) -> str:
    """Return a compact readable region label — no flag emojis (unreliable rendering)."""
    # Map region code → short dot-prefix label
    short = {
        # US
        "us-central1":              "● us-central1",
        "us-east1":                 "● us-east1",
        "us-east4":                 "● us-east4",
        "us-east5":                 "● us-east5",
        "us-south1":                "● us-south1",
        "us-west1":                 "● us-west1",
        "us-west2":                 "● us-west2",
        "us-west3":                 "● us-west3",
        "us-west4":                 "● us-west4",
        # Europe
        "europe-west1":             "● eu-west1",
        "europe-west2":             "● eu-west2",
        "europe-west3":             "● eu-west3",
        "europe-west4":             "● eu-west4",
        "europe-west6":             "● eu-west6",
        "europe-west8":             "● eu-west8",
        "europe-west9":             "● eu-west9",
        "europe-north1":            "● eu-north1",
        "europe-central2":          "● eu-central2",
        # Asia
        "asia-east1":               "● asia-east1",
        "asia-east2":               "● asia-east2",
        "asia-northeast1":          "● asia-ne1",
        "asia-northeast2":          "● asia-ne2",
        "asia-northeast3":          "● asia-ne3",
        "asia-southeast1":          "● asia-se1",
        "asia-southeast2":          "● asia-se2",
        "asia-south1":              "● asia-south1",
        "asia-south2":              "● asia-south2",
        # Other
        "northamerica-northeast1":  "● ca-montreal",
        "northamerica-northeast2":  "● ca-toronto",
        "southamerica-east1":       "● sa-east1",
        "southamerica-west1":       "● sa-west1",
        "australia-southeast1":     "● au-sydney",
        "australia-southeast2":     "● au-melbourne",
        "me-west1":                 "● me-west1",
        "me-central1":              "● me-central1",
        "africa-south1":            "● af-south1",
    }
    return short.get(region, f"● {region}")

def action_color(action: str) -> tuple:
    """Return (bg, border, text, symbol) for action type."""
    if action == "add":     return "#0d2818","#2ea043","#56d364","+"
    if action == "destroy": return "#2d0f0f","#f85149","#ff7b72","−"
    if action == "change":  return "#2d1f00","#d29922","#f0c84a","~"
    if action == "replace": return "#1a0a2e","#8957e5","#d2a8ff","±"
    return "#161b22","#30363d","#8b949e","?"

def _h(tag: str, style: str, content: str, cls: str = "") -> str:
    """Build a single-line HTML element — NO newlines so Streamlit markdown won't escape it."""
    c = f' class="{cls}"' if cls else ""
    return f'<{tag}{c} style="{style}">{content}</{tag}>'


def render_plan_card(plan_details: dict, is_destroy: bool = False,
                     has_warn: bool = False, in_state: set = None) -> str:
    """Return a single-line HTML string (no newlines/indentation) for Streamlit st.markdown."""
    in_state = in_state or set()
    summary  = plan_details.get("summary", "")
    adds     = plan_details.get("add",     [])
    changes  = plan_details.get("change",  [])
    destroys = plan_details.get("destroy", [])
    replaces = plan_details.get("replace", [])

    # Split adds into truly new vs already-in-state (will be auto-imported on apply)
    new_adds      = [r for r in adds if r["name"] not in in_state]
    existing_adds = [r for r in adds if r["name"] in in_state]

    # Detect side effects in a destroy plan (unexpected adds/replaces)
    _has_collateral = is_destroy and (
        plan_details.get("add", []) or plan_details.get("replace", [])
    )
    if _has_collateral:
        h_bg, h_col = "#3d1500", "#f85149"
        h_icon, h_text = "⚠️", "Destroy + unexpected side effects"
    elif is_destroy:
        h_bg, h_col = "#2d1f00", "#d29922"
        h_icon, h_text = "🗑️", "Destroy Plan"
    elif has_warn:
        h_bg, h_col = "#3d1500", "#f85149"
        h_icon, h_text = "⚠️", "Warning — plan includes destroy"
    else:
        h_bg, h_col = "#0d2818", "#3fb950"
        h_icon, h_text = "✅", "Plan Ready"

    s_col = "#f85149" if (has_warn or is_destroy) else "#3fb950"

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = (
        _h("span", "font-size:1.1rem", h_icon) +
        _h("span", f"color:{h_col};font-weight:700;font-size:0.85rem;margin-left:8px", h_text) +
        _h("span", f"margin-left:auto;color:{s_col};font-family:JetBrains Mono,monospace;font-size:0.72rem;font-weight:600", summary)
    )
    header_div = _h("div", f"background:{h_bg};padding:8px 14px;display:flex;align-items:center;border-bottom:1px solid #30363d", hdr)

    # ── Resource rows ─────────────────────────────────────────────────────────
    rows_html = ""

    # New resources (truly being added)
    for action, items in [("add", new_adds), ("change", changes), ("replace", replaces), ("destroy", destroys)]:
        bg, border, col, sym = action_color(action)
        for item in items:
            badge  = _h("span",
                f"background:{bg};border:1px solid {border};color:{col};"
                "font-family:JetBrains Mono,monospace;font-size:0.7rem;font-weight:700;"
                "padding:1px 7px;border-radius:4px;min-width:20px;text-align:center",
                sym)
            ico    = _h("span", "font-size:0.95rem", resource_icon(item["type"]))
            name   = _h("span",
                "color:#e6edf3;font-family:JetBrains Mono,monospace;font-weight:600;flex:1",
                item["name"])
            rtype  = _h("span",
                "color:#484f58;font-family:JetBrains Mono,monospace;font-size:0.68rem",
                item["type"])
            rflag  = region_flag(item["region"])
            dot_colored = rflag.replace("●", f'<span style="color:{col}">●</span>', 1)
            region = _h("span",
                "background:#161b22;border:1px solid #21262d;color:#8b949e;"
                "font-family:JetBrains Mono,monospace;font-size:0.65rem;"
                "padding:2px 8px;border-radius:10px;white-space:nowrap",
                dot_colored)
            row = _h("div",
                "display:flex;align-items:center;gap:10px;padding:7px 14px;"
                "border-bottom:1px solid #21262d;font-size:0.78rem",
                badge + ico + name + rtype + region)
            rows_html += row

    # Already-in-state resources (in HCL but already exist in GCP — will be auto-imported)
    if existing_adds:
        rows_html += _h("div",
            "padding:4px 14px;color:#484f58;font-size:0.65rem;"
            "font-family:JetBrains Mono,monospace;border-bottom:1px solid #161b22;"
            "background:#0a0d14",
            f"↳ {len(existing_adds)} already in GCP — will auto-import on apply:")
        for item in existing_adds:
            ico   = _h("span", "font-size:0.85rem", resource_icon(item["type"]))
            name  = _h("span",
                "color:#484f58;font-family:JetBrains Mono,monospace;font-size:0.75rem;flex:1",
                item["name"])
            rtype = _h("span",
                "color:#30363d;font-family:JetBrains Mono,monospace;font-size:0.65rem",
                item["type"])
            tag   = _h("span",
                "background:#161b22;border:1px solid #21262d;color:#484f58;"
                "font-family:JetBrains Mono,monospace;font-size:0.62rem;"
                "padding:1px 7px;border-radius:10px",
                "already exists")
            row = _h("div",
                "display:flex;align-items:center;gap:8px;padding:5px 14px;"
                "border-bottom:1px solid #161b22;opacity:0.6",
                ico + name + rtype + tag)
            rows_html += row

    if not rows_html:
        rows_html = _h("div", "padding:10px 14px;color:#484f58;font-size:0.78rem",
                       "No resource changes detected.")

    card = _h("div",
        "background:#0d1117;border:1px solid #30363d;border-radius:10px;"
        "overflow:hidden;margin:4px 0;font-family:Inter,sans-serif",
        header_div + rows_html)
    return card


def resource_icon(rtype: str) -> str:
    if "storage_bucket" in rtype:     return "🪣"
    if "compute_instance" in rtype:   return "🖥️"
    if "sql" in rtype:                return "🗄️"
    if "container_cluster" in rtype:  return "⎈"
    if "container_node" in rtype:     return "🔧"
    if "cloud_run" in rtype:          return "🏃"
    if "pubsub" in rtype:             return "📨"
    if "bigquery" in rtype:           return "📊"
    if "compute_network" in rtype:    return "🌐"
    if "compute_subnetwork" in rtype: return "🔗"
    if "compute_firewall" in rtype:   return "🛡️"
    if "forwarding_rule" in rtype:    return "⚖️"
    if "cloudfunctions" in rtype:     return "λ"
    if "iam" in rtype:                return "🔑"
    return "☁️"

# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────
PLAN_TTL_SECS = 5 * 60   # 5 minutes — revert main.tf if user doesn't apply

defaults = {
    "messages":       [],
    "agent_job_id":   None,
    "apply_job_id":   None,
    "state_push_job": None,
    "pending_input":  "",
    "ready_to_apply": False,
    "plan_ts":        None,
    "apply_targets":  None,
    "is_targeted":    False,
    "is_destroy":     False,
    "destroy_target": None,
    "thinking":       False,
    "show_logs":      True,
    "rollback_job_id": None,
    "is_rollback":    False,
    "rollback_version": None,
    # ── Voice ──────────────────────────────────────────────────────────────────
    "voice_job_id":    None,
    "voice_command":   "",    # final command ready to send
    "voice_model_ok":  None,
    "voice_model":     None,  # None = use VOICE_MODEL env default
    "voice_audio_bytes": None, # persisted bytes from st.audio_input across rerun
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

if st.session_state.agent_job_id:
    _pre = get_job(st.session_state.agent_job_id)
    _res = _pre.get("result") or {}
    if (_pre.get("status") == "done" and _res.get("status") == "success"
            and not st.session_state.apply_job_id):
        st.session_state.ready_to_apply = True
        st.session_state.plan_ts        = time.time()
        st.session_state.is_destroy     = _res.get("is_destroy", False)
        st.session_state.destroy_target = _res.get("destroy_target")
        st.session_state.apply_targets  = _res.get("apply_targets")
        st.session_state.is_targeted    = _res.get("is_targeted", False)
        st.session_state.is_rollback    = _res.get("is_rollback", False)
        st.session_state.rollback_version = _res.get("rollback_version")

# ── Rollback job watcher (runs plan, sets ready_to_apply) ────────────────────
if st.session_state.rollback_job_id and not st.session_state.ready_to_apply:
    _rj = get_job(st.session_state.rollback_job_id)
    _rr = _rj.get("result") or {}
    if _rj.get("status") == "done" and _rr.get("status") == "success":
        st.session_state.ready_to_apply   = True
        st.session_state.plan_ts          = time.time()
        st.session_state.is_destroy       = False
        st.session_state.is_rollback      = True
        st.session_state.rollback_version = _rr.get("rollback_version")
        st.session_state.agent_job_id     = st.session_state.rollback_job_id
        st.session_state.rollback_job_id  = None

if st.session_state.ready_to_apply and st.session_state.plan_ts:
    age_secs = time.time() - st.session_state.plan_ts
    if age_secs > PLAN_TTL_SECS:
        # Revert main.tf to pre-plan state — unapplied HCL changes are discarded
        reverted = TerraformTools.revert_to_snapshot()
        st.session_state.ready_to_apply = False
        st.session_state.plan_ts        = None
        st.session_state.agent_job_id   = None
        revert_note = " main.tf has been reverted to its previous state." if reverted else ""
        st.session_state.messages.append({
            "role": "assistant", "type": "warning",
            "content": (
                f"⏰ **Plan expired** — not applied within 5 minutes and has been discarded.{revert_note}\n\n"
                "Please re-run your request to generate a fresh plan."
            )
        })

# ─────────────────────────────────────────────────────────────────────────────
# TOP HEADER
# ─────────────────────────────────────────────────────────────────────────────
project_display = GCP_PROJECT_ID or "⚠️ GCP_PROJECT_ID not set"
st.markdown(f"""
<div style='display:flex; align-items:center; padding:10px 20px;
            border-bottom:1px solid #21262d; background:#0d1117;
            position:sticky; top:0; z-index:999;'>
    <span style='font-size:1.2rem; margin-right:10px'>☁️</span>
    <span style='color:#e6edf3; font-weight:700; font-size:1rem; letter-spacing:-0.01em'>AI SRE Agent</span>
    <span style='background:#0d2818; border:1px solid #2ea043; color:#3fb950;
                 font-size:0.65rem; padding:1px 8px; border-radius:20px;
                 margin-left:10px; font-weight:600'>● ONLINE</span>
    <span style='margin-left:auto; color:#484f58; font-size:0.72rem;
                 font-family:"JetBrains Mono"'>{GEMINI_MODEL} · GCP · Terraform</span>
    <span style='margin-left:12px; color:#79c0ff; font-size:0.68rem;
                 font-family:"JetBrains Mono"'>🏗️ {project_display}</span>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Remote state banner
# ─────────────────────────────────────────────────────────────────────────────
state_bucket = os.environ.get("TF_STATE_BUCKET", "")
if state_bucket:
    # Check lock status (cached)
    try:
        lock_info = get_state_lock_info()
    except Exception:
        lock_info = None

    if lock_info:
        lock_html = (
            f"<span style='background:#3d0000;border:1px solid #f85149;"
            f"color:#f85149;padding:1px 8px;border-radius:4px;font-weight:700'>"
            f"🔐 LOCKED by {lock_info['who']} · {lock_info['operation']}</span>"
        )
    else:
        lock_html = "<span style='color:#3fb950'>🔓 Unlocked</span>"

    st.markdown(
        f"<div style='background:#0d1a10;border-bottom:1px solid #2ea043;"
        f"padding:5px 20px;font-size:0.72rem;font-family:JetBrains Mono,monospace;"
        f"display:flex;align-items:center;gap:20px;'>"
        f"<span style='color:#3fb950;font-weight:700'>☁️ GCS Remote State</span>"
        f"<span style='color:#8b949e'>🪣 gs://{state_bucket}</span>"
        f"<span style='color:#8b949e'>📍 {GCP_DEFAULT_REGION}</span>"
        f"{lock_html}"
        f"</div>",
        unsafe_allow_html=True)

    # Lock warning banner
    if lock_info:
        col_l1, col_l2 = st.columns([4, 1])
        with col_l1:
            st.error(
                f"🔐 **State is locked** — {lock_info['who']} · "
                f"op: `{lock_info['operation']}` · ID: `{lock_info['id']}`\n\n"
                f"Apply/destroy operations are blocked until the lock is released.")
        with col_l2:
            if st.button("⚠️ Force Unlock", use_container_width=True):
                result = force_unlock_state(lock_info["id"])
                st.success(f"Unlock result: {result[:200]}")
                st.rerun()
else:
    st.markdown(
        "<div style='background:#2d1f00;border-bottom:1px solid #d29922;"
        "padding:5px 20px;font-size:0.72rem;color:#d29922;"
        "font-family:JetBrains Mono,monospace;'>"
        "⚠️ Local state only — set <b>TF_STATE_BUCKET</b> to enable GCS remote state"
        "</div>",
        unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
col_chat, col_right = st.columns([5, 2], gap="small")

# =============================================================================
# RIGHT PANEL
# =============================================================================
with col_right:
    st.markdown("<div style='padding:0.5rem 1rem 0 0.5rem'>", unsafe_allow_html=True)

    # ── LIVE AGENT / APPLY LOGS (always visible at top of right panel) ─────────
    active_jid = st.session_state.apply_job_id or st.session_state.agent_job_id
    right_logs = []
    if active_jid:
        right_logs = get_job(active_jid).get("logs", [])

    is_agent_running  = bool(st.session_state.agent_job_id and st.session_state.thinking)
    _aj = st.session_state.apply_job_id
    is_apply_running  = bool(_aj and get_job(_aj).get("status") == "running")

    log_label_text = (
        "🔄 Agent — thinking..." if is_agent_running else
        "⚙️ Apply — running..."  if is_apply_running else
        "📋 Last run logs"       if right_logs else
        "📋 Agent Logs"
    )
    label_col = "#3fb950" if is_agent_running else "#d29922" if is_apply_running else "#484f58"

    st.markdown(
        f"<div class='log-label' style='color:{label_col}'>{log_label_text}</div>",
        unsafe_allow_html=True)

    log_html = colorize_logs(right_logs) if right_logs else (
        "<span style='color:#21262d'>No logs yet — start a conversation</span>")
    st.markdown(
        f"<div class='log-box-live' id='live-log'>{log_html}</div>"
        "<script>var b=document.getElementById('live-log');"
        "if(b)b.scrollTop=b.scrollHeight;</script>",
        unsafe_allow_html=True)

    st.markdown("<div style='margin-top:10px'>", unsafe_allow_html=True)
    try:
        all_code = TerraformTools.read_code()
        all_res  = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', all_code)
    except Exception:
        all_code, all_res = "", []

    st.markdown("---")

    # Apply button removed from right panel — lives in chat bubbles only

    # Apply job status
    if st.session_state.apply_job_id:
        job    = get_job(st.session_state.apply_job_id)
        status = job.get("status", "running")
        result = job.get("result") or {}
        logs   = job.get("logs", [])
        if status == "running":
            st.info("⏳ Applying changes in GCP...")
            if logs:
                st.markdown(f'<div class="log-box">{colorize_logs(logs)}</div>', unsafe_allow_html=True)
            time.sleep(3); st.rerun()
        elif status in ("done", "error"):
            out     = result.get("output", result.get("message", ""))
            out_low = out.lower()
            is_destroyed = "destroy complete" in out_low
            is_applied   = "apply complete"   in out_low
            # Primary success signal: job status + terraform completion keyword
            is_real_success = (result.get("status") == "success") and (is_destroyed or is_applied)
            # Detect backend config errors (need restart)
            needs_reinit = any(x in out_low for x in ["reconfigure", "migrate-state"])                            and not (is_destroyed or is_applied)
            # Detect credential errors specifically for a better hint
            needs_auth   = any(x in out_low for x in
                               ["application default credentials", "could not find default credentials",
                                "permission denied", "403"])                            and not (is_destroyed or is_applied)

            if is_real_success:
                label = "💣 Destroyed!" if is_destroyed else "✅ Applied successfully!"
                st.success(label)
                lines = [l for l in out.splitlines() if any(
                    x in l for x in ["Apply complete", "Destroy complete", "Resources:"])]
                summary = "\n".join(lines[:3]) if lines else out[-300:]
                chat_content = ("💣 **Destroyed!**" if is_destroyed else "✅ **Applied!**")
                chat_content += "\n```\n" + summary.strip() + "\n```"
                st.session_state.messages.append({
                    "role": "assistant", "type": "success", "content": chat_content
                })
            else:
                # Split full output from the error tail (last 50 lines)
                TAIL_MARKER = "\n~~~ERROR_TAIL~~~\n"
                if TAIL_MARKER in out:
                    full_out, error_tail = out.split(TAIL_MARKER, 1)
                else:
                    full_out  = out
                    # Fallback: last 40 lines is the error zone
                    lines_all = out.strip().splitlines()
                    error_tail = "\n".join(lines_all[-40:]) if len(lines_all) > 40 else out

                # Classify error type from the tail (where real errors live)
                tail_low = error_tail.lower()
                # Check if this is one of our rich diagnostic messages (starts with emoji + keyword)
                is_diagnostic = any(out.lstrip().startswith(p) for p in
                                    ["🔐", "🪣", "🔑", "❌ Init failed"])

                if needs_reinit:
                    err_label = "⚠️ Backend not initialised"
                    hint = "💡 Run: `docker compose restart`"
                elif "403" in tail_low or "access denied" in tail_low or is_diagnostic:
                    err_label = "🔐 GCS Permission Error"
                    hint = ""
                elif needs_auth or any(x in tail_low for x in
                                       ["application default credentials",
                                        "could not find default credentials",
                                        "credentials", "authentication"]):
                    err_label = "🔐 GCP authentication failed"
                    hint = "💡 Check **GOOGLE_CREDENTIALS** or **GOOGLE_APPLICATION_CREDENTIALS** in your `.env` file"
                elif result.get("status") == "error":
                    err_label = "❌ Apply failed"
                    hint = ""
                else:
                    err_label = "❌ Apply did not complete"
                    hint = ""

                st.error(err_label)
                if hint:
                    st.info(hint)

                # Show the error tail prominently — this is where the real error is
                st.markdown("""
                <div style='background:#1a0a0a; border:1px solid #f85149; border-radius:8px;
                             padding:4px 0; margin:6px 0;'>
                    <div style='padding:6px 14px; font-size:0.72rem; color:#f85149;
                                font-family:"JetBrains Mono"; border-bottom:1px solid #2d0f0f;'>
                        ⬇️  Error detail (last 40 lines)
                    </div>
                </div>""", unsafe_allow_html=True)
                st.code(error_tail.strip(), language="bash")

                with st.expander("📄 Full output", expanded=False):
                    st.code(full_out.strip(), language="bash")

                st.session_state.messages.append({
                    "role": "assistant", "type": "error",
                    "content": "❌ **" + err_label + "**\n```\n" + error_tail.strip()[-800:] + "\n```"
                })
            st.session_state.apply_job_id = None
            st.rerun()

    st.markdown("---")

    # ── Terraform files ───────────────────────────────────────────────────────
    with st.expander("📁 main.tf", expanded=False):
        st.code(all_code if all_code else "# empty", language="hcl")

    with st.expander("☁️ Remote State (GCS)", expanded=False):
        try:
            bucket_env = os.environ.get("TF_STATE_BUCKET", "")
            bpath = f"{TF_DIR}/backend.tf"

            if bucket_env:
                state_data  = read_tfstate()
                n_resources = len(state_data["resources"])
                serial      = state_data["raw"].get("serial", "—")
                tf_ver      = state_data["raw"].get("terraform_version", "—")

                st.markdown(
                    f"<div style='display:flex;flex-wrap:wrap;gap:14px;"
                    f"font-family:JetBrains Mono,monospace;font-size:0.72rem;"
                    f"color:#8b949e;padding:4px 0'>"
                    f"<span>🪣 <b style='color:#e6edf3'>gs://{bucket_env}</b></span>"
                    f"<span>📦 {n_resources} resources</span>"
                    f"<span>🔢 serial {serial}</span>"
                    f"<span>🏗️ tf {tf_ver}</span>"
                    f"</div>",
                    unsafe_allow_html=True)

                bcol1, bcol2 = st.columns(2)
                with bcol1:
                    if st.button("⬆️ Push State → GCS", use_container_width=True,
                                 help="Upload local terraform.tfstate to the GCS remote backend"):
                        st.session_state.state_push_job = start_state_push_job()
                        st.rerun()
                with bcol2:
                    gcs_url = f"https://console.cloud.google.com/storage/browser/{bucket_env}"
                    st.markdown(
                        f"<a href='{gcs_url}' target='_blank' style='"
                        "display:block;text-align:center;padding:6px 0;background:#161b22;"
                        "border:1px solid #30363d;border-radius:6px;color:#79c0ff;"
                        "font-size:0.78rem;text-decoration:none'>🔗 GCS Console</a>",
                        unsafe_allow_html=True)

                # Push job status
                push_jid = st.session_state.get("state_push_job")
                if push_jid:
                    pjob = get_job(push_jid)
                    pstatus = pjob.get("status")
                    presult = (pjob.get("result") or {})
                    if pstatus == "running":
                        st.info("⏳ Pushing state to GCS…")
                        time.sleep(1); st.rerun()
                    elif pstatus == "done":
                        st.success(presult.get("message", "State pushed."))
                        st.session_state.state_push_job = None
                    elif pstatus == "error":
                        st.error(presult.get("message", "Push failed."))
                        st.session_state.state_push_job = None

                if os.path.exists(bpath):
                    with st.expander("📄 backend.tf", expanded=False):
                        st.code(open(bpath).read(), language="hcl")
            else:
                st.warning("TF_STATE_BUCKET not set — using local state.")
                if os.path.exists(bpath):
                    st.code(open(bpath).read(), language="hcl")
        except Exception as e:
            st.caption(f"State panel error: {e}")

    with st.expander("🔌 provider.tf", expanded=False):
        try:
            ptf_path    = f"{TF_DIR}/provider.tf"
            ptf_content = open(ptf_path).read() if os.path.exists(ptf_path) else "# not created yet"
            st.code(ptf_content, language="hcl")
            aliases = get_existing_provider_aliases()
            if aliases:
                alias_html = "".join(
                    f'<div class="res-card" style="margin:3px 0">'
                    f'<span style="color:#79c0ff;font-family:JetBrains Mono;font-size:0.75rem">{a}</span>'
                    f'<span class="res-type">{r}</span></div>'
                    for a, r in aliases.items()
                )
                st.markdown(f"**Active aliases:**{alias_html}", unsafe_allow_html=True)
        except Exception as e:
            st.caption(f"Error: {e}")

    with st.expander("🗃️ tfstate", expanded=False):
        sp = f"{TF_DIR}/terraform.tfstate"
        if os.path.exists(sp):
            try:
                s = json.load(open(sp))
                for r in s.get("resources", []):
                    st.markdown(
                        f'<div class="res-card running">'
                        f'<span>{resource_icon(r.get("type",""))}</span>'
                        f'<span class="res-name">{r.get("name")}</span>'
                        f'<span class="res-type">{r.get("type")}</span></div>',
                        unsafe_allow_html=True)
                st.caption(f"terraform v{s.get('terraform_version')} · serial {s.get('serial')}")
            except Exception as e:
                st.caption(f"Error: {e}")
        else:
            st.caption("No state file yet.")

    if st.session_state.agent_job_id or st.session_state.ready_to_apply:
        job_id = st.session_state.agent_job_id
        if job_id:
            res      = (get_job(job_id).get("result") or {})
            plan_txt = res.get("plan", "")
            if plan_txt:
                with st.expander("🔍 Terraform Plan", expanded=True):
                    for line in plan_txt.splitlines():
                        if any(x in line for x in ["to add", "to change", "to destroy"]):
                            has_dest = "to destroy" in line and "0 to destroy" not in line
                            color    = "#f85149" if has_dest else "#3fb950"
                            st.markdown(
                                f'<p style="color:{color}; font-family:JetBrains Mono; '
                                f'font-size:0.85rem; font-weight:600; margin:4px 0">'
                                f'▸ {line.strip()}</p>', unsafe_allow_html=True)
                            break
                    st.code(plan_txt, language="bash")

    # ── Audit Trail ───────────────────────────────────────────────────────────
    with st.expander("📋 Audit Trail", expanded=False):
        audit_entries = read_audit_log(max_entries=50)
        if not audit_entries:
            st.caption("No actions logged yet.")
        else:
            _ACTION_COLOR = {
                "apply": "#3fb950", "plan": "#4d9eff", "destroy": "#f85149",
                "apply_target": "#79c0ff", "error": "#ff4d6d", "unknown": "#484f58",
            }
            for entry in audit_entries:
                action   = entry.get("type", entry.get("action", "unknown"))
                status   = entry.get("action", "done")
                user     = entry.get("user", "—")
                ts       = (entry.get("ts") or "")[:19].replace("T", " ")
                color    = _ACTION_COLOR.get(action, "#8b949e")
                err_col  = "#f85149" if status == "error" else color
                added    = entry.get("resources_added", [])
                destroyed= entry.get("resources_destroyed", [])
                fixed    = entry.get("auto_fixed", [])
                cost     = entry.get("cost_total")
                findings = entry.get("security_findings", 0)
                details  = []
                if added:     details.append(f"+{len(added)}")
                if destroyed: details.append(f"💣{len(destroyed)}")
                if fixed:     details.append(f"🔒{len(fixed)}")
                if cost:      details.append(f"${cost:.0f}/mo")
                if findings:  details.append(f"⚠️{findings}")
                detail_str = " · ".join(details) or (entry.get("message","")[:120])
                st.markdown(
                    f"<div style='font-family:JetBrains Mono,monospace;font-size:0.67rem;"
                    f"padding:5px 8px;margin-bottom:3px;border-left:3px solid {err_col};"
                    f"background:#0d1117;border-radius:0 4px 4px 0'>"
                    f"<span style='color:{err_col};font-weight:700'>{action.upper()}</span>"
                    f" <span style='color:#484f58'>{user} · {ts}</span>"
                    f"{'<br><span style=color:#8b949e>' + detail_str + '</span>' if detail_str else ''}"
                    f"</div>", unsafe_allow_html=True)

    # ── Version History & Rollback ─────────────────────────────────────────────
    with st.expander("⏪ Version History", expanded=False):
        versions = get_version_history()
        if not versions:
            st.caption("No versions saved yet — versions are created after each successful apply.")
        else:
            # Show rollback job status if running
            if st.session_state.rollback_job_id:
                _rj = get_job(st.session_state.rollback_job_id)
                if _rj.get("status") == "running":
                    st.info("⏳ Preparing rollback plan…")
                elif (_rj.get("result") or {}).get("status") == "error":
                    st.error((_rj.get("result") or {}).get("message", "Rollback failed"))
                    st.session_state.rollback_job_id = None

            _ACTION_ICON = {
                "apply": "🟢", "destroy": "🔴",
                "apply_target": "🟡", "rollback": "⏪", "system": "⚙️",
            }
            for v in versions:
                vid   = v["version_id"]
                ts_v  = v.get("ts","")[:19].replace("T"," ")
                label = v.get("label", "—")
                user  = v.get("user", "—")
                nres  = v.get("resource_count", 0)
                icon  = _ACTION_ICON.get(v.get("action",""), "📌")
                serial= v.get("state_serial")
                is_current = (vid == versions[0]["version_id"])  # newest = current

                current_badge = (
                    "&nbsp;&nbsp;<span style='background:#1f3a5f;color:#4d9eff;"
                    "font-size:0.55rem;padding:1px 5px;border-radius:3px'>CURRENT</span>"
                    if is_current else ""
                )
                border_col  = "#4d9eff" if is_current else "#21262d"
                res_plural  = "s" if nres != 1 else ""
                serial_str  = f" · serial {serial}" if serial else ""
                st.markdown(
                    f"<div style='background:#0d1117;border:1px solid #21262d;"
                    f"border-left:3px solid {border_col};"
                    f"border-radius:0 6px 6px 0;padding:8px 10px;margin-bottom:6px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                    f"<span style='font-family:JetBrains Mono;font-size:0.65rem;color:#8b949e'>"
                    f"{icon} {ts_v}{current_badge}"
                    f"</span>"
                    f"<span style='font-family:JetBrains Mono;font-size:0.6rem;color:#484f58'>"
                    f"{nres} resource{res_plural}{serial_str}"
                    f"</span></div>"
                    f"<div style='font-size:0.72rem;color:#c9d1d9;margin-top:3px'>{label}</div>"
                    f"<div style='font-size:0.62rem;color:#484f58'>by {user} · id {vid}</div>"
                    f"</div>", unsafe_allow_html=True)

                # Resources tooltip
                res_list = v.get("resources", [])
                if res_list:
                    with st.expander(f"  📋 {nres} resource(s) in this version", expanded=False):
                        for r in res_list:
                            st.markdown(
                                f"<span style='font-family:JetBrains Mono;font-size:0.65rem;"
                                f"color:#8b949e'>{r.get('type','?')}."
                                f"<b style='color:#c9d1d9'>{r.get('name','?')}</b></span>",
                                unsafe_allow_html=True)

                if not is_current and not st.session_state.ready_to_apply:
                    if st.button(f"⏪ Roll back to this version",
                                 key=f"rb_{vid}", use_container_width=True):
                        user_now = get_current_user()
                        st.session_state.rollback_job_id = start_rollback_job(vid, user=user_now)
                        st.session_state.thinking = True
                        st.session_state.messages.append({
                            "role": "user",
                            "content": f"⏪ Rolling back to version `{vid}` ({label})"
                        })
                        st.rerun()
                elif is_current:
                    st.caption("  ↳ This is the current version")
                st.markdown("---" if not is_current else "", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

# =============================================================================
# LEFT PANEL — Chat  (full-height, buttons outside scroll container)
# =============================================================================
with col_chat:
    # ── Dynamically size chat container so input dock is always visible ────────
    st.markdown("""
    <style>
    /* Remove sticky — doesn't work inside Streamlit columns */
    .input-dock { position: relative !important; }
    </style>
    <script>
    (function() {
        function setChatHeight() {
            var vh = window.innerHeight;
            var inputDockH = 160;  /* tabs + textarea + clear button */
            var headerH    = 80;
            var h = Math.max(300, vh - headerH - inputDockH);
            var style = document.getElementById('_chat_h_style');
            if (!style) {
                style = document.createElement('style');
                style.id = '_chat_h_style';
                document.head.appendChild(style);
            }
            style.textContent =
                '[data-testid="stVerticalBlockBorderWrapper"] > div > div[data-testid="stVerticalBlock"] ' +
                '> div[style*="overflow"] { max-height: ' + h + 'px !important; height: ' + h + 'px !important; }';
        }
        setChatHeight();
        window.addEventListener('resize', setChatHeight);
    })();
    </script>""", unsafe_allow_html=True)

    chat_area = st.container(height=int(os.environ.get("CHAT_HEIGHT", "520")), border=False)

    with chat_area:
        if not st.session_state.messages:
            st.markdown("""
            <div style='text-align:center; padding:4rem 2rem; color:#484f58;'>
                <div style='font-size:2.5rem; margin-bottom:1rem'>☁️</div>
                <p style='color:#8b949e; font-weight:600; font-size:0.95rem; margin:0'>
                    AI SRE Agent — Google Cloud
                </p>
                <p style='font-size:0.8rem; color:#484f58; margin-top:0.5rem'>
                    Describe any GCP infrastructure — I'll write the Terraform,<br>
                    validate it, and show you the plan before applying.
                </p>
                <div style='display:flex; flex-wrap:wrap; gap:8px; justify-content:center; margin-top:1.5rem;'>
                    <div style='background:#161b22; border:1px solid #30363d; border-radius:8px;
                                padding:8px 14px; font-size:0.78rem; color:#8b949e;'>🪣 GCS bucket in europe-west2</div>
                    <div style='background:#161b22; border:1px solid #30363d; border-radius:8px;
                                padding:8px 14px; font-size:0.78rem; color:#8b949e;'>🖥️ Compute VM in asia-southeast1</div>
                    <div style='background:#161b22; border:1px solid #30363d; border-radius:8px;
                                padding:8px 14px; font-size:0.78rem; color:#8b949e;'>⎈ GKE cluster + node pool</div>
                    <div style='background:#161b22; border:1px solid #30363d; border-radius:8px;
                                padding:8px 14px; font-size:0.78rem; color:#8b949e;'>🗑️ Remove resource X</div>
                </div>
            </div>""", unsafe_allow_html=True)
        else:
            for idx, msg in enumerate(st.session_state.messages):
                mtype   = msg.get("type", "")
                action  = msg.get("action", "")
                is_last = idx == len(st.session_state.messages) - 1

                if msg["role"] == "user":
                    text = msg["content"].replace("<","&lt;").replace(">","&gt;")
                    st.markdown(
                        f'<div class="msg-row-user">'
                        f'<div class="bubble-user">{text}</div></div>',
                        unsafe_allow_html=True)
                else:
                    pd_data    = msg.get("plan_details")
                    has_warn   = msg.get("has_warn", False)
                    is_dest_msg= msg.get("is_destroy", False)

                    if pd_data:
                        # ── Rollback banner ─────────────────────────────────
                        if msg.get("is_rollback") and msg.get("rollback_version"):
                            rv = msg["rollback_version"]
                            rv_res = rv.get("resource_count", 0)
                            rv_ts  = rv.get("ts","")[:19].replace("T"," ")
                            st.markdown(
                                f'<div class="msg-row-assistant"><div style="max-width:90%;width:100%;'
                                f'background:rgba(167,139,250,0.07);border:1px solid rgba(167,139,250,0.3);'
                                f'border-radius:8px;padding:10px 14px;margin-bottom:6px">'
                                f'<div style="font-family:JetBrains Mono;font-size:0.7rem;'
                                f'color:#a78bfa;font-weight:700;margin-bottom:4px">⏪ ROLLBACK PLAN</div>'
                                f'<div style="font-size:0.78rem;color:#c9d1d9">'
                                f'Restoring to version <code style="color:#a78bfa">{rv.get("version_id","")}</code>'
                                f' — <b>{rv.get("label","")}</b></div>'
                                f'<div style="font-size:0.7rem;color:#8b949e;margin-top:3px">'
                                f'{rv_ts} · {rv_res} resource{"s" if rv_res!=1 else ""} · by {rv.get("user","—")}'
                                f'</div></div></div>',
                                unsafe_allow_html=True)

                        # ── Rich plan card ──────────────────────────────────
                        _in_state = get_created_resources()
                        card_html = render_plan_card(pd_data, is_dest_msg, has_warn, _in_state)
                        wrapper = ('<div class="msg-row-assistant">'
                                   '<div style="max-width:90%;width:100%">'
                                   + card_html +
                                   '</div></div>')
                        st.markdown(wrapper, unsafe_allow_html=True)

                        # ── Cost estimate panel ─────────────────────────────
                        cost = msg.get("cost_estimate")
                        if cost and cost.get("items"):
                            sev_badge = ""
                            total = cost.get("total_monthly", 0)
                            col_cost = "#3fb950" if total < 50 else "#d29922" if total < 200 else "#f85149"
                            rows_html = ""
                            for item in cost["items"]:
                                p = item.get("monthly_usd")
                                p_str = f"${p:.2f}/mo" if p is not None else "N/A"
                                size  = f' <span style="color:#484f58">({item["size"]})</span>' if item.get("size") else ""
                                rows_html += (
                                    f'<div style="display:flex;justify-content:space-between;'
                                    f'padding:3px 0;border-bottom:1px solid #21262d;font-size:0.75rem">'
                                    f'<span style="color:#e6edf3;font-family:JetBrains Mono">'
                                    f'{item["name"]}</span>{size}'
                                    f'<span style="color:{col_cost};font-family:JetBrains Mono;'
                                    f'font-weight:600">{p_str}</span></div>'
                                )
                            st.markdown(
                                f'<div class="msg-row-assistant"><div style="max-width:90%;width:100%;'
                                f'background:#0d1117;border:1px solid #21262d;border-radius:8px;'
                                f'padding:10px 14px;margin-top:4px">'
                                f'<div style="font-size:0.7rem;color:#484f58;text-transform:uppercase;'
                                f'letter-spacing:0.06em;margin-bottom:6px">💰 Cost Estimate</div>'
                                f'{rows_html}'
                                f'<div style="display:flex;justify-content:space-between;'
                                f'padding:5px 0 0 0;margin-top:4px;font-size:0.78rem;font-weight:700">'
                                f'<span style="color:#8b949e">Total estimate</span>'
                                f'<span style="color:{col_cost};font-family:JetBrains Mono">'
                                f'${total:.2f}/month</span></div>'
                                f'<div style="font-size:0.64rem;color:#484f58;margin-top:4px">'
                                f'{cost.get("disclaimer","")}</div>'
                                f'</div></div>',
                                unsafe_allow_html=True)

                        # ── Security audit panel ────────────────────────────
                        audit = msg.get("security_audit")
                        if audit:
                            sev_colors = {"HIGH": "#f85149", "MEDIUM": "#d29922", "LOW": "#79c0ff"}
                            sev_bg     = {"HIGH": "#2d0f0f", "MEDIUM": "#2d1f00", "LOW": "#0c1e38"}
                            auto_fixed = {f["id"] for f in msg.get("auto_fixed", [])}
                            audit_rows = ""
                            for f in audit:
                                sc  = sev_colors.get(f["severity"], "#8b949e")
                                sb  = sev_bg.get(f["severity"], "#161b22")
                                was_fixed = f["id"] in auto_fixed
                                fixed_badge = (
                                    '<span style="background:#1f6feb;color:#e6edf3;font-size:0.6rem;'
                                    'font-weight:700;padding:1px 6px;border-radius:3px;'
                                    'font-family:JetBrains Mono;margin-left:6px">AUTO-FIXED ✓</span>'
                                    if was_fixed else ""
                                )
                                opacity = "opacity:0.5;" if was_fixed else ""
                                audit_rows += (
                                    f'<div style="background:{sb};border-left:3px solid {sc};'
                                    f'border-radius:4px;padding:7px 10px;margin:5px 0;{opacity}">'
                                    f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">'
                                    f'<span style="background:{sc};color:#0d1117;font-size:0.6rem;'
                                    f'font-weight:700;padding:1px 6px;border-radius:3px;font-family:JetBrains Mono">'
                                    f'{f["severity"]}</span>'
                                    f'<span style="color:#e6edf3;font-size:0.75rem;font-weight:600">'
                                    f'[{f["id"]}] {f["title"]}</span>'
                                    f'<span style="color:#484f58;font-size:0.68rem;font-family:JetBrains Mono">'
                                    f'{f["resource_name"]}</span>'
                                    f'{fixed_badge}</div>'
                                    f'<div style="color:#8b949e;font-size:0.72rem;margin-bottom:3px">'
                                    f'{f["detail"]}</div>'
                                    f'<div style="color:#3fb950;font-size:0.68rem;font-family:JetBrains Mono">'
                                    f'Fix: {f["fix"]}</div>'
                                    f'</div>'
                                )
                            high_n   = sum(1 for f in audit if f["severity"] == "HIGH")
                            fixed_n  = len(auto_fixed)
                            remain_n = len(audit) - fixed_n
                            header_color = "#f85149" if (high_n and not all(
                                f["id"] in auto_fixed for f in audit if f["severity"]=="HIGH")) else "#3fb950" if fixed_n else "#d29922"
                            st.markdown(
                                f'<div class="msg-row-assistant"><div style="max-width:90%;width:100%;'
                                f'background:#0d1117;border:1px solid #21262d;border-radius:8px;'
                                f'padding:10px 14px;margin-top:4px">'
                                f'<div style="font-size:0.7rem;color:{header_color};text-transform:uppercase;'
                                f'letter-spacing:0.06em;margin-bottom:6px">'
                                f'🔒 Security Audit — {len(audit)} finding(s)'
                                f'{"  ✅ " + str(fixed_n) + " auto-fixed" if fixed_n else ""}'
                                f'{"  ⚠️ " + str(remain_n) + " need review" if remain_n else ""}'
                                f'</div>'
                                f'{audit_rows}'
                                f'</div></div>',
                                unsafe_allow_html=True)
                    else:
                        # ── Query result card ──────────────────────────────
                        if mtype == "query" and msg.get("content"):
                            qm    = msg.get("query_meta", {})
                            qtype = qm.get("type", "query")
                            icon_map = {
                                "list_vms": "🖥️", "vm_info": "🖥️", "vm_metrics": "📈",
                                "list_buckets": "🪣", "bucket_info": "🪣", "bucket_size": "🪣",
                                "logs": "📋", "run_services": "🚀", "billing": "💰",
                                "generic": "🔍",
                            }
                            icon  = icon_map.get(qtype, "🔍")
                            label = qtype.replace("_", " ").upper()
                            content = msg["content"]
                            # Render markdown-style tables and bold
                            content = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', content)
                            content = re.sub(r'`([^`\n]+)`',
                                r'<code style="background:#21262d;padding:1px 5px;border-radius:3px;'
                                r'font-family:JetBrains Mono;font-size:0.78rem">\1</code>', content)
                            content = content.replace("\n", "<br>")
                            st.markdown(
                                f'<div class="msg-row-assistant">'
                                f'<div class="bubble-assistant" style="border-left:3px solid #7c3aed;'
                                f'background:linear-gradient(135deg,#0d1117 0%,#13071f 100%)">'
                                f'<div style="font-family:JetBrains Mono;font-size:0.58rem;'
                                f'color:#7c3aed;margin-bottom:5px;letter-spacing:0.08em">'
                                f'{icon} {label}</div>'
                                f'{content}'
                                f'</div></div>',
                                unsafe_allow_html=True)
                        # ── Plain text bubble ──────────────────────────────
                        elif msg.get("content"):
                            extra = ""
                            if mtype == "warning": extra = " bubble-warning"
                            elif mtype == "error":   extra = " bubble-error"
                            elif mtype == "success": extra = " bubble-success"

                            rendered = msg["content"]
                            rendered = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', rendered)
                            rendered = re.sub(r'`([^`\n]+)`',
                                r'<code style="background:#21262d;padding:1px 5px;border-radius:3px;'
                                r'font-family:JetBrains Mono;font-size:0.8rem">\1</code>', rendered)
                            rendered = re.sub(
                                r'```(?:\w+)?\n?(.*?)```',
                                r'<pre style="background:#0d1117;border:1px solid #30363d;'
                                r'border-radius:6px;padding:8px 12px;overflow-x:auto;'
                                r'font-family:JetBrains Mono;font-size:0.75rem;margin:6px 0">\1</pre>',
                                rendered, flags=re.DOTALL)
                            rendered = rendered.replace("\n", "<br>")
                            st.markdown(
                                f'<div class="msg-row-assistant">'
                                f'<div class="bubble-assistant{extra}">{rendered}</div></div>',
                                unsafe_allow_html=True)

                    # Track last message with action button
                    if action and is_last and not st.session_state.thinking:
                        st.session_state["_last_action"]       = action
                        st.session_state["_last_action_idx"]   = idx
                        st.session_state["_last_action_pcount"]= msg.get("pending_count", 0)
                    elif not action and is_last:
                        st.session_state["_last_action"] = None

        # ── Thinking indicator inside chat_area ───────────────────────────────
        if st.session_state.thinking and st.session_state.agent_job_id:
            job  = get_job(st.session_state.agent_job_id)
            logs = job.get("logs", [])
            with chat_area:
                step       = logs[-1] if logs else "Analyzing request..."
                step_clean = re.sub(r'\[.{8}\] ', '', step)
                st.markdown(f"""
                <div class="msg-row-assistant">
                    <div class="bubble-assistant" style="color:#8b949e; min-width:180px">
                        <span class="thinking-dot"></span>
                        <span class="thinking-dot"></span>
                        <span class="thinking-dot"></span>
                        <span style="font-size:0.75rem; margin-left:6px">{step_clean}</span>
                    </div>
                </div>""", unsafe_allow_html=True)

    # ─────────────────────────────────────────────────────────────────────────
    # ACTION BUTTONS — rendered OUTSIDE chat_area so they are always visible
    # ─────────────────────────────────────────────────────────────────────────
    _action     = st.session_state.get("_last_action")
    _action_idx = st.session_state.get("_last_action_idx", 0)
    _pcount     = st.session_state.get("_last_action_pcount", 0)

    # Derive _action from session state if message loop didn't set it
    # (happens on the rerun immediately after result handler fires)
    if not _action and st.session_state.ready_to_apply:
        if st.session_state.is_destroy:
            _action = "destroy"
        else:
            _action = "apply"

    # Get security findings from the last plan message for the fix button
    _last_security = []
    if st.session_state.messages:
        for m in reversed(st.session_state.messages):
            if m.get("security_audit") is not None:
                _last_security = m.get("security_audit", [])
                break
    # patchable field set by Gemini audit — True = Gemini can auto-fix it
    _fixable_ids = [f["id"] for f in _last_security if f.get("patchable", False)]

    if (_action or st.session_state.ready_to_apply) and not st.session_state.thinking:
        st.markdown("<div class='action-bar'>", unsafe_allow_html=True)

        # Countdown timer
        if st.session_state.plan_ts:
            age     = time.time() - st.session_state.plan_ts
            left    = max(0, PLAN_TTL_SECS - age)
            mins    = int(left // 60)
            secs    = int(left % 60)
            pct     = left / PLAN_TTL_SECS
            bar_col = "#3fb950" if pct > 0.4 else "#d29922" if pct > 0.15 else "#f85149"
            st.markdown(
                f"<div style='font-family:JetBrains Mono,monospace;font-size:0.68rem;"
                f"color:{bar_col};padding:2px 0 4px 2px'>"
                f"⏱ {mins:02d}:{secs:02d} — apply or main.tf will be reverted"
                f"</div>", unsafe_allow_html=True)

        # Show Fix Security button if there are fixable findings
        if _fixable_ids and _action in ("apply", "warn_destroy"):
            high_fixable = [f for f in _last_security
                            if f["severity"] == "HIGH" and f["id"] in _fixable_ids]
            badge = f"{len(high_fixable)} HIGH · " if high_fixable else ""
            st.markdown(
                f"<div style='font-family:JetBrains Mono,monospace;font-size:0.68rem;"
                f"color:#d29922;padding:2px 0 4px 2px'>"
                f"🔒 {len(_fixable_ids)} security issue(s) can be auto-fixed "
                f"({badge}{len(_last_security)} total) — click to patch before applying"
                f"</div>", unsafe_allow_html=True)
            fix_cols = st.columns([2, 8])
            with fix_cols[0]:
                fix_label = f"🔒 Fix {len(_fixable_ids)} Security Issue(s)"
                if st.button(fix_label, key="action_fix_security", use_container_width=True):
                    # Send "fix security ALL" as next agent message
                    fix_msg = f"fix security issues: {', '.join(_fixable_ids)}"
                    st.session_state.messages.append({"role": "user", "content": fix_msg})
                    st.session_state.agent_job_id   = start_agent_job(
                        st.session_state.messages.copy(), user=get_current_user())
                    st.session_state.ready_to_apply = False
                    st.session_state.plan_ts        = None
                    st.session_state.thinking       = True
                    st.session_state["_last_action"]= None
                    st.rerun()

        btn_cols = st.columns([2, 2, 6])

        # ── Shared discard helper ─────────────────────────────────────────
        def _discard_plan(idx):
            """Revert main.tf snapshot, clear all plan state, remove action tag."""
            TerraformTools.revert_to_snapshot()
            st.session_state.ready_to_apply  = False
            st.session_state.plan_ts         = None
            st.session_state.apply_targets   = None
            st.session_state.is_targeted     = False
            st.session_state.is_destroy      = False
            st.session_state.destroy_target  = None
            st.session_state.is_rollback     = False
            st.session_state.rollback_version= None
            st.session_state["_last_action"] = None
            if idx < len(st.session_state.messages):
                st.session_state.messages[idx].pop("action", None)
                # Append a discard notice to chat
                st.session_state.messages.append({
                    "role": "assistant", "type": "info",
                    "content": "🗑️ Plan discarded — main.tf reverted. Nothing was applied to GCP."
                })

        if _action == "apply":
            with btn_cols[0]:
                st.markdown('<div class="btn-apply">', unsafe_allow_html=True)
                label = f"🚀 Apply ({_pcount} new)" if _pcount else "🚀 Apply Changes"
                if st.session_state.is_rollback:
                    rv = st.session_state.rollback_version or {}
                    label = f"⏪ Confirm Rollback → {rv.get('version_id','')[:15]}"
                if st.button(label, key="action_apply", use_container_width=True):
                    st.session_state.apply_job_id     = start_apply_job(user=get_current_user())
                    st.session_state.ready_to_apply   = False
                    st.session_state.plan_ts          = None
                    st.session_state.is_rollback      = False
                    st.session_state.rollback_version = None
                    st.session_state["_last_action"]  = None
                    if _action_idx < len(st.session_state.messages):
                        st.session_state.messages[_action_idx].pop("action", None)
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with btn_cols[1]:
                if st.button("❌ Discard Plan", key="action_discard_apply",
                             use_container_width=True):
                    _discard_plan(_action_idx)
                    st.rerun()

        elif _action == "destroy":
            with btn_cols[0]:
                st.markdown('<div class="btn-destroy">', unsafe_allow_html=True)
                if st.button("💣 Confirm Destroy", key="action_destroy", use_container_width=True):
                    st.session_state.apply_job_id   = start_apply_job(
                        is_destroy=True,
                        destroy_target=st.session_state.destroy_target,
                        user=get_current_user())
                    st.session_state.ready_to_apply = False
                    st.session_state.plan_ts        = None
                    st.session_state["_last_action"]= None
                    if _action_idx < len(st.session_state.messages):
                        st.session_state.messages[_action_idx].pop("action", None)
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with btn_cols[1]:
                if st.button("❌ Cancel", key="action_cancel_d", use_container_width=True):
                    _discard_plan(_action_idx)
                    st.rerun()

        elif _action == "warn_destroy":
            with btn_cols[0]:
                st.markdown('<div class="btn-destroy">', unsafe_allow_html=True)
                if st.button("⚠️ Apply Anyway", key="action_warn", use_container_width=True):
                    st.session_state.apply_job_id   = start_apply_job(user=get_current_user())
                    st.session_state.ready_to_apply = False
                    st.session_state.plan_ts        = None
                    st.session_state["_last_action"]= None
                    if _action_idx < len(st.session_state.messages):
                        st.session_state.messages[_action_idx].pop("action", None)
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with btn_cols[1]:
                if st.button("❌ Cancel", key="action_cancel_w", use_container_width=True):
                    _discard_plan(_action_idx)
                    st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)

    # ── Agent result handler ──────────────────────────────────────────────────
    if st.session_state.thinking and st.session_state.agent_job_id:
        job    = get_job(st.session_state.agent_job_id)
        status = job.get("status", "running")
        result = job.get("result") or {}

        if status == "running":
            time.sleep(2); st.rerun()

        elif status in ("done", "error"):
            st.session_state.thinking = False
            r = result.get("status", "error")
            if r == "success":
                plan         = result.get("plan", "")
                plan_details = result.get("plan_details") or parse_plan_details(plan, TerraformTools.read_code())
                is_dest      = result.get("is_destroy", False)
                has_unexpected_destroy = bool(
                    re.search(r'[1-9]\d* to destroy', plan)) and not is_dest
                # Also flag collateral side effects in a targeted destroy plan
                if result.get("collateral_warning"):
                    has_unexpected_destroy = True

                if has_unexpected_destroy:
                    mtype, action_type = "warning", "warn_destroy"
                elif is_dest:
                    mtype, action_type = "warning", "destroy"
                else:
                    mtype, action_type = "success", "apply"

                _in_state_names = get_created_resources()
                _truly_new = [r for r in plan_details.get("add", [])
                              if r["name"] not in _in_state_names]
                st.session_state.messages.append({
                    "role": "assistant",
                    "type": mtype,
                    "content": "",          # rendered via plan_card key
                    "action": action_type,
                    "plan_details": plan_details,
                    "is_destroy": is_dest,
                    "has_warn": has_unexpected_destroy,
                    "pending_count": len(_truly_new) if not is_dest else 0,
                    "cost_estimate":  result.get("cost_estimate"),
                    "security_audit": result.get("security_audit"),
                    "auto_fixed":     result.get("auto_fixed", []),
                    "is_rollback":    result.get("is_rollback", False),
                    "rollback_version": result.get("rollback_version"),
                })
                # Set ready_to_apply NOW — before clearing agent_job_id —
                # so the top-of-page watcher doesn't need to re-fire next render
                st.session_state.ready_to_apply   = True
                st.session_state.plan_ts          = time.time()
                st.session_state.is_destroy       = is_dest
                st.session_state.destroy_target   = result.get("destroy_target")
                st.session_state.apply_targets    = result.get("apply_targets")
                st.session_state.is_targeted      = result.get("is_targeted", False)
                st.session_state.is_rollback      = result.get("is_rollback", False)
                st.session_state.rollback_version = result.get("rollback_version")
                st.session_state["_last_action"]        = action_type
                st.session_state["_last_action_idx"]    = len(st.session_state.messages) - 1
                st.session_state["_last_action_pcount"] = len(_truly_new) if not is_dest else 0
            elif r == "query":
                st.session_state.messages.append({
                    "role":    "assistant",
                    "type":    "query",
                    "content": result.get("message", ""),
                    "query_meta": result.get("query_meta", {}),
                })
            elif r == "info":
                st.session_state.messages.append({
                    "role": "assistant", "type": "info",
                    "content": result.get("message", "ℹ️ No changes needed.")})
            else:
                st.session_state.messages.append({
                    "role": "assistant", "type": "error",
                    "content": f"❌ {result.get('message','Something went wrong.')}"})
            st.session_state.agent_job_id = None
            st.rerun()

    # ── Input dock (tabs: Chat / Voice / Diagram) ─────────────────────────────
    st.markdown("<div class='input-dock'>", unsafe_allow_html=True)

    # JavaScript: Enter to send in chat textarea + browser TTS for voice responses
    st.markdown("""
    <script>
    (function() {
        // ── Enter key → Send button ──────────────────────────────────────────
        function hookTextarea() {
            var ta = document.querySelector('textarea[data-testid="stTextArea"]') ||
                     document.querySelector('textarea');
            if (!ta || ta._hooked) return;
            ta._hooked = true;
            ta.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    var btn = Array.from(document.querySelectorAll('button')).find(
                        b => b.innerText.trim() === '➤');
                    if (btn) btn.click();
                }
            });
        }
        hookTextarea();
        var iv = setInterval(function() {
            hookTextarea();
            if (document.querySelector('textarea._hooked')) clearInterval(iv);
        }, 300);

        // ── TTS: speak queued text via Web Speech API ────────────────────────
        window._sreSpeak = function(text) {
            if (!window.speechSynthesis) return;
            window.speechSynthesis.cancel();
            var utt = new SpeechSynthesisUtterance(text);
            utt.rate = 1.05; utt.pitch = 1.0; utt.volume = 1.0;
            // Prefer a natural English voice
            var voices = window.speechSynthesis.getVoices();
            var preferred = voices.find(v => v.lang === 'en-US' && v.localService) ||
                            voices.find(v => v.lang.startsWith('en')) || voices[0];
            if (preferred) utt.voice = preferred;
            window.speechSynthesis.speak(utt);
        };
        // Pre-load voices (Chrome lazy-loads them)
        if (window.speechSynthesis) {
            window.speechSynthesis.getVoices();
            window.speechSynthesis.onvoiceschanged = function() {
                window.speechSynthesis.getVoices();
            };
        }
    })();
    </script>""", unsafe_allow_html=True)

    tab_chat, tab_voice = st.tabs(["💬 Chat", "🎙️ Voice"])

    # ── Tab: Chat ─────────────────────────────────────────────────────────────
    with tab_chat:
        inp_col, btn_col = st.columns([10, 1])
        with inp_col:
            user_input = st.text_area(
                "msg", label_visibility="collapsed",
                placeholder="create vm micro_vm e2-micro us-central1  (Enter to send, Shift+Enter for newline)",
                height=62, key="chat_input",
                value=st.session_state.voice_command)
        with btn_col:
            st.markdown('<div class="btn-send">', unsafe_allow_html=True)
            send = st.button("➤", key="btn_send", use_container_width=True,
                             disabled=st.session_state.thinking)
            st.markdown('</div>', unsafe_allow_html=True)

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.session_state.messages:
                if st.button("🗑️ Clear chat", use_container_width=True):
                    for k in ["messages","agent_job_id","apply_job_id",
                               "ready_to_apply","plan_ts","is_destroy","destroy_target",
                               "thinking","apply_targets","is_targeted",
                               "voice_command","voice_job_id"]:
                        st.session_state[k] = [] if k == "messages" else \
                                              False if isinstance(st.session_state.get(k), bool) else \
                                              "" if k == "voice_command" else None
                    st.rerun()
        with bcol2:
            if st.button("🔄 Refresh", use_container_width=True):
                st.rerun()

    # ── Tab: Voice ────────────────────────────────────────────────────────────
    with tab_voice:
        # ── Model selector ────────────────────────────────────────────────
        _VOICE_MODELS = ["gemini-2.5-flash"]
        if st.session_state.voice_model is None:
            _default_idx = (_VOICE_MODELS.index(VOICE_MODEL)
                            if VOICE_MODEL in _VOICE_MODELS else 0)
        else:
            _default_idx = (_VOICE_MODELS.index(st.session_state.voice_model)
                            if st.session_state.voice_model in _VOICE_MODELS else 0)

        _sel_model = st.selectbox(
            "Voice model", options=_VOICE_MODELS, index=_default_idx,
            key="voice_model_select", label_visibility="collapsed")
        st.session_state.voice_model = _sel_model

        # Status dot
        _vm_ok = st.session_state.voice_model_ok
        _dot   = "🟢" if _vm_ok is True else "🔴" if _vm_ok is False else "⚪"
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.6rem;"
            f"color:#484f58;margin:-4px 0 6px 2px'>{_dot} {_sel_model}</div>",
            unsafe_allow_html=True)

        # ── Audio recorder ────────────────────────────────────────────────
        audio_data = st.audio_input(
            "Record", key="voice_recorder", label_visibility="collapsed")

        # Persist bytes immediately — audio_data becomes None on next rerun
        # after a button click, so we store bytes in session_state.
        if audio_data is not None:
            raw_bytes = audio_data.read()
            if raw_bytes:
                st.session_state.voice_audio_bytes = raw_bytes

        _jid   = st.session_state.voice_job_id
        _cmd   = st.session_state.voice_command
        _abytes = st.session_state.voice_audio_bytes

        # ── Transcribe button ─────────────────────────────────────────────
        if _abytes and not _jid and not _cmd:
            if st.button("🎙️ Transcribe", key="voice_transcribe",
                         use_container_width=True):
                st.session_state.voice_audio_bytes = None  # consumed
                st.session_state.voice_job_id = start_voice_job(
                    _abytes, "audio/wav",
                    model=st.session_state.voice_model,
                    user=get_current_user())
                st.rerun()

        # ── Poll transcription job ────────────────────────────────────────
        if _jid:
            _vj  = get_job(_jid)
            _vst = _vj.get("status", "running")
            _vr  = _vj.get("result") or {}

            if _vst == "running":
                st.markdown(
                    "<div style='font-family:JetBrains Mono;font-size:0.72rem;"
                    "color:#4d9eff;padding:8px 0 4px 0'>⏳ Transcribing…</div>",
                    unsafe_allow_html=True)
                time.sleep(1); st.rerun()

            elif _vst == "done":
                st.session_state.voice_job_id = None
                if _vr.get("status") == "success":
                    transcript = _vr.get("transcription", "")
                    command    = _vr.get("command", transcript)
                    spoken_rsp = _vr.get("spoken_response", "")
                    model_used = _vr.get("model_used", _sel_model)

                    # Guard: if command still looks like raw JSON, extract from it
                    if command.strip().startswith("{"):
                        import json as _j
                        try:
                            _d = _j.loads(command)
                            command = _d.get("command") or _d.get("transcription") or transcript
                        except Exception:
                            # Partial JSON — grab first quoted value
                            _m = re.search(r'"(?:command|transcription)"\s*:\s*"([^"]+)"', command)
                            command = _m.group(1) if _m else transcript

                    st.session_state.voice_command  = command or transcript
                    st.session_state.voice_model_ok = True
                    if model_used in _VOICE_MODELS:
                        st.session_state.voice_model = model_used
                    if spoken_rsp:
                        st.markdown(
                            f"<script>window._sreSpeak({json.dumps(spoken_rsp)});</script>",
                            unsafe_allow_html=True)
                else:
                    st.session_state.voice_model_ok = False
                    st.error(_vr.get("message", "Transcription failed — please try again."))
                st.rerun()

        # ── Transcript card + Send/Discard ────────────────────────────────
        _cmd = st.session_state.voice_command
        if _cmd:
            st.markdown(
                f"<div style='background:#0d1117;border:1px solid #21262d;"
                f"border-left:3px solid #4d9eff;border-radius:0 6px 6px 0;"
                f"padding:10px 14px;margin:6px 0 10px 0'>"
                f"<div style='font-family:JetBrains Mono;font-size:0.6rem;"
                f"color:#484f58;margin-bottom:3px'>🎙️ HEARD</div>"
                f"<div style='font-size:0.85rem;color:#c9d1d9;font-weight:500'>{_cmd}</div>"
                f"</div>",
                unsafe_allow_html=True)

            s_col, d_col = st.columns([3, 2])
            with s_col:
                if st.button("➤ Send to Agent", key="voice_send",
                             use_container_width=True,
                             disabled=st.session_state.thinking):
                    cmd_to_send = st.session_state.voice_command
                    st.session_state.voice_command     = ""
                    st.session_state.voice_audio_bytes = None
                    st.session_state.messages.append(
                        {"role": "user", "content": f"🎙️ {cmd_to_send}"})
                    st.session_state.agent_job_id   = start_agent_job(
                        st.session_state.messages.copy(), user=get_current_user())
                    st.session_state.apply_job_id   = None
                    st.session_state.ready_to_apply = False
                    st.session_state.thinking       = True
                    st.rerun()
            with d_col:
                if st.button("🗑️ Discard", key="voice_clear",
                             use_container_width=True):
                    st.session_state.voice_command     = ""
                    st.session_state.voice_audio_bytes = None
                    st.rerun()

        # Hint
        if not _cmd and not _jid and not _abytes:
            st.markdown(
                "<div style='font-family:JetBrains Mono;font-size:0.6rem;color:#30363d;"
                "margin-top:10px;line-height:2'>"
                "e.g. <span style='color:#3d444d'>create vm prod-api e2-medium us-central1</span>"
                " · <span style='color:#3d444d'>remove all test buckets</span>"
                "</div>",
                unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Handle chat send ──────────────────────────────────────────────────────
    _submit_text = st.session_state.get("chat_input", "").strip()

    if send and _submit_text:
        # Delete the widget key so Streamlit resets it to empty on next render
        # (setting pending_input="" is not enough — Streamlit preserves keyed widget state)
        if "chat_input" in st.session_state:
            del st.session_state["chat_input"]
        st.session_state.messages.append({"role": "user", "content": _submit_text})
        st.session_state.agent_job_id   = start_agent_job(st.session_state.messages.copy(), user=get_current_user())
        st.session_state.apply_job_id   = None
        st.session_state.ready_to_apply = False
        st.session_state.is_destroy     = False
        st.session_state.thinking       = True
        st.rerun()
