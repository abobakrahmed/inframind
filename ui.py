import re
import time
import json
import os
from datetime import datetime
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
    start_drift_job, start_drift_fix_job,
    start_voice_job,
    start_deploy_job,
)

# ── Block direct browser access (allow only from auth proxy) ─────────────────
_INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
if _INTERNAL_SECRET:
    try:
        _got = st.context.headers.get("X-Internal-Secret", "")
        # Also accept via query param (used by WebSocket upgrade path)
        if not _got:
            _got = st.query_params.get("_int", "")
    except Exception:
        _got = ""
    if _got != _INTERNAL_SECRET:
        st.set_page_config(page_title="403 Forbidden", layout="centered")
        st.markdown(
            "<div style='text-align:center;padding:20vh 0;font-family:monospace'>"
            "<div style='font-size:3rem'>🔒</div>"
            "<div style='font-size:1.4rem;color:#f85149;margin:8px 0'>403 Forbidden</div>"
            "<div style='color:#6e7681'>Access only via the authorised entry point.</div>"
            "</div>", unsafe_allow_html=True)
        st.stop()
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="AI SRE Agent — GCP", layout="wide", page_icon="☁️",
                   initial_sidebar_state="collapsed")

def get_current_user() -> str:
    """Read the authenticated username injected by the auth proxy header."""
    try:
        headers = st.context.headers
        return headers.get("X-Authenticated-User", "unknown")
    except Exception:
        return "unknown"

def get_current_role() -> str:
    """Read the role injected by the auth proxy (admin | viewer)."""
    try:
        return st.context.headers.get("X-User-Role", "viewer")
    except Exception:
        return "viewer"

def require_admin(action: str = "this action") -> bool:
    """
    Returns True if current user is admin.
    Shows a blocking error in the UI if not — call before any destructive action.
    """
    if get_current_role() == "admin":
        return True
    st.error(f"🔒 **Access denied** — {action} requires admin role. "
             f"Contact your administrator.")
    return False

# ── Monitor Agent helpers ─────────────────────────────────────────────────────
_REDIS_URL = os.environ.get("REDIS_URL", "")
_ui_redis  = None

def _get_ui_redis():
    global _ui_redis
    if _ui_redis:
        return _ui_redis
    if not _REDIS_URL:
        return None
    try:
        import redis as _r
        _ui_redis = _r.from_url(_REDIS_URL, decode_responses=True)
        _ui_redis.ping()
    except Exception:
        _ui_redis = None
    return _ui_redis

def _poll_monitor_alerts():
    """
    Non-blocking: drain unread alerts from Redis list 'sre:alerts:ui'
    and heartbeat from 'sre:monitor:hb'. Called once per Streamlit rerun.
    Monitor agent pushes to list; UI pops and stores in session_state.
    """
    rc = _get_ui_redis()
    if not rc:
        return
    try:
        # Drain up to 20 new alerts per render cycle
        for _ in range(20):
            raw = rc.lpop("sre:alerts:ui")
            if not raw:
                break
            alert = json.loads(raw)
            st.session_state.monitor_alerts.append(alert)
        # Heartbeat (latest only)
        hb_raw = rc.get("sre:monitor:hb")
        if hb_raw:
            st.session_state.monitor_hb = json.loads(hb_raw)
    except Exception:
        pass

def _trigger_monitor_run():
    """Tell the monitor agent to run a check immediately."""
    rc = _get_ui_redis()
    if rc:
        try:
            rc.publish("sre:monitor:commands",
                       json.dumps({"action": "run_now"}))
        except Exception:
            pass

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

*, *::before, *::after { box-sizing: border-box; }
body, .stApp { background: #09090B !important; font-family: 'Inter', sans-serif; }
.block-container { padding: 0 !important; max-width: 100% !important; }
section[data-testid="stSidebar"], header[data-testid="stHeader"],
#MainMenu, footer { display: none !important; }
.stTextArea label, .stTextInput label { display: none; }

::-webkit-scrollbar { width: 3px; height: 3px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #3F3F46; border-radius: 3px; }

/* ── Messages ── */
.msg-row-user      { display:flex; justify-content:flex-end;  margin:4px 12px; }
.msg-row-assistant { display:flex; justify-content:flex-start; margin:4px 12px; }

.bubble-user {
    background: #4F46E5;
    color: #EEF2FF;
    border-radius: 16px 16px 4px 16px;
    padding: 10px 15px;
    max-width: 68%;
    font-size: 0.875rem;
    line-height: 1.6;
    word-break: break-word;
}
.bubble-assistant {
    background: #18181B;
    color: #E4E4E7;
    border: 0.5px solid #3F3F46;
    border-radius: 16px 16px 16px 4px;
    padding: 10px 15px;
    max-width: 80%;
    font-size: 0.875rem;
    line-height: 1.6;
    word-break: break-word;
}
.bubble-warning { background:#1C1400; border-color:#78350F !important; color:#FDE68A; }
.bubble-error   { background:#1A0505; border-color:#7F1D1D !important; color:#FCA5A5; }
.bubble-success { background:#052E1A; border-color:#14532D !important; color:#86EFAC; }

/* ── Thinking dots ── */
.thinking-dot {
    display:inline-block; width:5px; height:5px;
    background:#6366F1; border-radius:50%; margin:0 2px;
    animation: blink 1.4s infinite;
}
.thinking-dot:nth-child(2) { animation-delay:0.2s; }
.thinking-dot:nth-child(3) { animation-delay:0.4s; }
@keyframes blink { 0%,80%,100%{opacity:0.15} 40%{opacity:1} }

/* ── Log box ── */
.log-box, .log-box-live {
    background: #09090B;
    border: 0.5px solid #27272A;
    border-radius: 8px;
    padding: 10px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: #71717A;
    max-height: 200px;
    height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
}
.log-line-ok   { color: #4ADE80; }
.log-line-err  { color: #F87171; }
.log-line-warn { color: #FCD34D; }
.log-line-info { color: #818CF8; }

/* ── Resource cards ── */
.res-card {
    background: #18181B;
    border: 0.5px solid #27272A;
    border-radius: 8px;
    padding: 8px 12px;
    margin: 4px 0;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.78rem;
}
.res-card.pending { border-color: #78350F; }
.res-card.running { border-color: #14532D; }
.res-name { color: #F4F4F5; font-family:'JetBrains Mono'; font-weight:500; flex:1; }
.res-type { color: #52525B; font-family:'JetBrains Mono'; font-size:0.68rem; }

/* ── Badges ── */
.badge {
    display:inline-block; padding:1px 8px; border-radius:20px;
    font-size:0.65rem; font-weight:600; letter-spacing:0.04em;
    font-family:'JetBrains Mono';
}
.badge-green  { background:#052E1A; color:#4ADE80; border:0.5px solid #14532D; }
.badge-yellow { background:#1C1400; color:#FCD34D; border:0.5px solid #78350F; }
.badge-red    { background:#1A0505; color:#F87171; border:0.5px solid #7F1D1D; }
.badge-indigo { background:#1E1B4B; color:#818CF8; border:0.5px solid #3730A3; }

/* ── Buttons ── */
.stButton > button {
    background: #18181B !important;
    color: #A1A1AA !important;
    border: 0.5px solid #3F3F46 !important;
    border-radius: 8px !important;
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    padding: 6px 14px !important;
    transition: all 0.12s !important;
    width: 100% !important;
}
.stButton > button:hover  { background: #27272A !important; color: #E4E4E7 !important; border-color: #52525B !important; }
.stButton > button:active { transform: scale(0.99) !important; }
.stButton > button:disabled { opacity: 0.3 !important; }

.btn-apply   > button { background: #1E1B4B !important; border-color: #4338CA !important; color: #818CF8 !important; }
.btn-apply   > button:hover { background: #312E81 !important; color: #C7D2FE !important; }
.btn-destroy > button { background: #1A0505 !important; border-color: #991B1B !important; color: #FCA5A5 !important; }
.btn-destroy > button:hover { background: #7F1D1D !important; }
.btn-send    > button { background: #4F46E5 !important; border-color: #4338CA !important; color: #EEF2FF !important; height: 62px !important; font-size: 1rem !important; }
.btn-send    > button:hover { background: #4338CA !important; }

/* ── Textarea ── */
.stTextArea textarea {
    background: #18181B !important;
    color: #E4E4E7 !important;
    border: 0.5px solid #3F3F46 !important;
    border-radius: 10px !important;
    font-size: 0.875rem !important;
    resize: none !important;
    line-height: 1.55 !important;
    font-family: 'Inter', sans-serif !important;
}
.stTextArea textarea:focus {
    border-color: #4F46E5 !important;
    box-shadow: 0 0 0 2px rgba(79,70,229,0.2) !important;
}
.stTextArea textarea::placeholder { color: #52525B !important; }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: #09090B !important;
    gap: 2px !important;
    padding: 0 !important;
    border-bottom: 0.5px solid #27272A !important;
}
.stTabs [data-baseweb="tab"] {
    background: transparent !important;
    border: none !important;
    color: #71717A !important;
    font-size: 0.8rem !important;
    font-weight: 500 !important;
    padding: 8px 14px !important;
    border-radius: 0 !important;
    border-bottom: 2px solid transparent !important;
}
.stTabs [aria-selected="true"] {
    background: transparent !important;
    color: #E4E4E7 !important;
    border-bottom: 2px solid #6366F1 !important;
}
.stTabs [data-baseweb="tab-panel"] {
    background: #09090B !important;
    padding: 10px 0 0 0 !important;
}

/* ── Expanders ── */
.streamlit-expanderHeader {
    background: #18181B !important;
    border: 0.5px solid #27272A !important;
    border-radius: 8px !important;
    color: #A1A1AA !important;
    font-size: 0.78rem !important;
    font-family: 'JetBrains Mono', monospace !important;
    padding: 8px 12px !important;
}
details[open] .streamlit-expanderHeader {
    border-radius: 8px 8px 0 0 !important;
    border-bottom: 0.5px solid #3F3F46 !important;
}
.streamlit-expanderContent {
    background: #0F0F11 !important;
    border: 0.5px solid #27272A !important;
    border-top: none !important;
    border-radius: 0 0 8px 8px !important;
}

/* ── Metrics ── */
[data-testid="metric-container"] {
    background: #18181B !important;
    border: 0.5px solid #27272A !important;
    border-radius: 8px !important;
    padding: 10px 14px !important;
}
[data-testid="metric-container"] label { color: #71717A !important; font-size: 0.68rem !important; text-transform: uppercase; letter-spacing: 0.06em; }
[data-testid="stMetricValue"] { color: #F4F4F5 !important; font-size: 1.5rem !important; font-weight: 600 !important; }

/* ── Code blocks ── */
.stCodeBlock { border-radius: 8px !important; font-size: 0.75rem !important; }
code { background: #18181B !important; color: #818CF8 !important; padding: 1px 5px; border-radius: 4px; font-size: 0.82em; }

/* ── Alerts ── */
.stSuccess { background: #052E1A !important; border-left: 2px solid #16A34A !important; color: #86EFAC !important; }
.stWarning { background: #1C1400 !important; border-left: 2px solid #D97706 !important; color: #FDE68A !important; }
.stError   { background: #1A0505 !important; border-left: 2px solid #DC2626 !important; color: #FCA5A5 !important; }
.stInfo    { background: #0C0A1E !important; border-left: 2px solid #4F46E5 !important; color: #C7D2FE !important; }

hr { border-color: #27272A !important; margin: 0.5rem 0 !important; }

/* ── Input dock ── */
.input-dock {
    position: relative;
    background: #09090B;
    padding: 8px 0 4px;
    border-top: 0.5px solid #27272A;
}

/* ── Action bar ── */
.action-bar {
    padding: 6px 0 2px;
    background: #09090B;
    border-top: 0.5px solid #27272A;
}

/* ── Log label ── */
.log-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.62rem;
    color: #52525B;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin-bottom: 3px;
}

section.main { height: 100vh; overflow: hidden; }
section.main > div.block-container { padding-top: 0 !important; padding-bottom: 0 !important; max-width: 100% !important; }
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
    "drift_job_id":    None,
    "drift_result":    None,
    "drift_fix_job_id": None,
    "is_rollback":    False,
    "rollback_version": None,
    # ── Voice ──────────────────────────────────────────────────────────────────
    "voice_job_id":    None,
    "voice_command":   "",    # final command ready to send
    "voice_model_ok":  None,
    "voice_model":     None,  # None = use VOICE_MODEL env default
    "voice_audio_bytes": None, # persisted bytes from st.audio_input across rerun
    # ── Monitor Agent ──────────────────────────────────────────────────────────
    "monitor_alerts":  [],
    "monitor_hb":      None,
    # ── Deploy panel ───────────────────────────────────────────────────────────
    "deploy_job_id":   None,
    "deploy_repo_url": "",
    "deploy_branch":   "main",
    "deploy_app_name": "",
    "deploy_env":      "production",
    "deploy_prompt":   "",
    "deploy_token":    "",
    "deploy_history":  [],   # list of recent deploy records for history panel
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
project_display = GCP_PROJECT_ID[:22] + "…" if len(GCP_PROJECT_ID) > 22 else GCP_PROJECT_ID or "⚠ set GCP_PROJECT_ID"
_current_user = get_current_user()
_current_role = get_current_role()

# State & lock info
state_bucket = os.environ.get("TF_STATE_BUCKET", "")
try:
    lock_info = get_state_lock_info() if state_bucket else None
except Exception:
    lock_info = None

_lock_pill = (
    "<span style='background:#3D0000;border:0.5px solid #991B1B;color:#FCA5A5;"
    "font-size:0.6rem;padding:1px 7px;border-radius:20px;font-weight:600'>🔐 LOCKED</span>"
    if lock_info else
    "<span style='background:#052E1A;border:0.5px solid #14532D;color:#4ADE80;"
    "font-size:0.6rem;padding:1px 7px;border-radius:20px;font-weight:600'>● LIVE</span>"
)
_role_pill = (
    "<span style='background:#1E1B4B;border:0.5px solid #3730A3;color:#818CF8;"
    "font-size:0.6rem;padding:1px 7px;border-radius:20px;font-weight:600'>ADMIN</span>"
    if _current_role == "admin" else
    "<span style='background:#18181B;border:0.5px solid #3F3F46;color:#71717A;"
    "font-size:0.6rem;padding:1px 7px;border-radius:20px;font-weight:600'>VIEWER</span>"
)
_region_short = GCP_DEFAULT_REGION

st.markdown(f"""
<div style='display:flex;align-items:center;padding:0 20px;height:52px;
            border-bottom:0.5px solid #27272A;background:#09090B;
            position:sticky;top:0;z-index:999;gap:10px;'>
  <div style='display:flex;align-items:center;gap:8px;min-width:0'>
    <div style='width:28px;height:28px;background:#4F46E5;border-radius:8px;
                display:flex;align-items:center;justify-content:center;
                font-size:14px;flex-shrink:0'>⬡</div>
    <span style='color:#F4F4F5;font-weight:600;font-size:0.9rem;white-space:nowrap'>AI SRE</span>
  </div>
  <div style='width:0.5px;height:20px;background:#27272A;flex-shrink:0'></div>
  <div style='display:flex;align-items:center;gap:6px;min-width:0'>
    <span style='color:#52525B;font-size:0.72rem;font-family:"JetBrains Mono";white-space:nowrap'>
      {project_display}
    </span>
    <span style='color:#52525B;font-size:0.68rem'>/</span>
    <span style='color:#71717A;font-size:0.7rem;font-family:"JetBrains Mono"'>{_region_short}</span>
  </div>
  {_lock_pill}
  <div style='flex:1'></div>
  <span style='color:#3F3F46;font-size:0.68rem;font-family:"JetBrains Mono";white-space:nowrap'>{GEMINI_MODEL}</span>
  <div style='width:0.5px;height:20px;background:#27272A;flex-shrink:0'></div>
  <div style='display:flex;align-items:center;gap:6px'>
    <div style='width:22px;height:22px;background:#1E1B4B;border-radius:50%;
                display:flex;align-items:center;justify-content:center;
                font-size:10px;color:#818CF8;font-weight:600'>
      {_current_user[0].upper() if _current_user and _current_user != "unknown" else "?"}
    </div>
    <span style='color:#71717A;font-size:0.75rem'>{_current_user}</span>
    {_role_pill}
  </div>
  <a href="/logout" style='background:#18181B;border:0.5px solid #3F3F46;color:#71717A;
     font-size:0.7rem;padding:4px 10px;border-radius:6px;text-decoration:none;
     font-weight:500;white-space:nowrap;transition:all 0.12s'
     onmouseover="this.style.borderColor='#F87171';this.style.color='#F87171'"
     onmouseout="this.style.borderColor='#3F3F46';this.style.color='#71717A'">
    Sign out
  </a>
</div>
""", unsafe_allow_html=True)

# State banner (only when lock or no bucket)
if lock_info:
    col_l1, col_l2 = st.columns([4, 1])
    with col_l1:
        st.error(
            f"🔐 **State locked** — {lock_info['who']} · "
            f"`{lock_info['operation']}` · ID: `{lock_info['id']}`")
    with col_l2:
        if st.button("Force unlock", use_container_width=True):
            result = force_unlock_state(lock_info["id"])
            st.success(result[:200])
            st.rerun()
elif not state_bucket:
    st.markdown(
        "<div style='background:#1C1400;border-bottom:0.5px solid #78350F;"
        "padding:5px 20px;font-size:0.72rem;color:#FCD34D;"
        "font-family:JetBrains Mono,monospace;'>"
        "⚠ Local state only — set TF_STATE_BUCKET for GCS remote state"
        "</div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
col_chat, col_right = st.columns([5, 2], gap="small")

# =============================================================================
# RIGHT PANEL
with col_right:
    st.markdown("<div style='height:100%;overflow-y:auto;padding:8px 6px;background:#09090B'>", unsafe_allow_html=True)

    # ── Live activity bar (always on top when active) ─────────────────────────
    active_jid = st.session_state.apply_job_id or st.session_state.agent_job_id
    right_logs = []
    if active_jid:
        right_logs = get_job(active_jid).get("logs", [])

    is_agent_running = bool(st.session_state.agent_job_id and st.session_state.thinking)
    _aj = st.session_state.apply_job_id
    is_apply_running = bool(_aj and get_job(_aj).get("status") == "running")

    if right_logs or is_agent_running or is_apply_running:
        log_label_text = (
            "Agent  thinking…" if is_agent_running else
            "Apply  running…"  if is_apply_running else
            "Agent logs")
        _dot_color = "#818CF8" if is_agent_running else "#4ADE80" if is_apply_running else "#52525B"
        st.markdown(
            f"<div style='font-family:JetBrains Mono;font-size:0.62rem;color:{_dot_color};"
            f"text-transform:uppercase;letter-spacing:0.07em;margin-bottom:3px'>"
            f"● {log_label_text}</div>",
            unsafe_allow_html=True)
        st.markdown(
            f'<div class="log-box">{colorize_logs(right_logs)}</div>',
            unsafe_allow_html=True)
        if is_agent_running or is_apply_running:
            time.sleep(0.8); st.rerun()
        st.markdown("<div style='margin:6px 0'></div>", unsafe_allow_html=True)

    # ── 4-tab context panel ───────────────────────────────────────────────────
    _tab_files, _tab_monitor, _tab_history, _tab_drift, _tab_deploy = st.tabs(
        ["Files", "Monitor", "History", "Drift", "Deploy"])

    # ═══════════════════════════════════════════════════════════════════════
    # TAB 1 — FILES (main.tf, provider.tf, tfstate, plan)
    # ═══════════════════════════════════════════════════════════════════════
    with _tab_files:
        # ── main.tf ──────────────────────────────────────────────────────
        with st.expander("main.tf", expanded=False):
            hcl = TerraformTools.read_code()
            if hcl.strip():
                st.code(hcl, language="hcl")
            else:
                st.caption("Empty — no resources yet.")

        # ── provider.tf ──────────────────────────────────────────────────
        with st.expander("provider.tf", expanded=False):
            try:
                ptf = open(f"{TF_DIR}/provider.tf").read()
                st.code(ptf, language="hcl")
            except Exception:
                st.caption("Not generated yet.")

        # ── Remote state ──────────────────────────────────────────────────
        if state_bucket:
            with st.expander(f"Remote state  gs://{state_bucket[:20]}", expanded=False):
                tf_state = read_tfstate()
                if tf_state:
                    resources = tf_state.get("resources", [])
                    st.markdown(
                        f"<div style='font-size:0.68rem;color:#71717A;font-family:JetBrains Mono;"
                        f"margin-bottom:6px'>{len(resources)} resource(s) · "
                        f"serial {tf_state.get('serial',0)} · "
                        f"tf {tf_state.get('terraform_version','')}</div>",
                        unsafe_allow_html=True)
                    for r in resources:
                        inst   = (r.get("instances") or [{}])[0]
                        attrs  = inst.get("attributes", {})
                        rname  = attrs.get("name") or r.get("name", "")
                        st.markdown(
                            f"<div class='res-card'>"
                            f"<span>{resource_icon(r.get('type',''))}</span>"
                            f"<span class='res-name'>{rname}</span>"
                            f"<span class='res-type'>{r.get('type','').replace('google_','')}</span>"
                            f"</div>", unsafe_allow_html=True)
                else:
                    st.caption("No state file yet.")

        # ── Terraform plan (when ready) ───────────────────────────────────
        if st.session_state.agent_job_id or st.session_state.ready_to_apply:
            job_id = st.session_state.agent_job_id
            if job_id:
                res      = (get_job(job_id).get("result") or {})
                plan_txt = res.get("plan", "")
                if plan_txt:
                    with st.expander("Terraform plan", expanded=True):
                        for line in plan_txt.splitlines():
                            if any(x in line for x in ["to add", "to change", "to destroy"]):
                                has_dest = "to destroy" in line and "0 to destroy" not in line
                                color    = "#F87171" if has_dest else "#4ADE80"
                                plan_line = line.strip()
                                st.markdown(
                                    f'<p style="color:{color};font-family:JetBrains Mono;'
                                    f'font-size:0.82rem;font-weight:600;margin:4px 0">'
                                    f'▸ {plan_line}</p>', unsafe_allow_html=True)
                                break
                        st.code(plan_txt, language="bash")

    # ═══════════════════════════════════════════════════════════════════════
    # TAB 2 — MONITOR
    # ═══════════════════════════════════════════════════════════════════════
    with _tab_monitor:
        _poll_monitor_alerts()
        _hb   = st.session_state.monitor_hb
        _mals = st.session_state.monitor_alerts

        if _hb:
            _err_rate = _hb.get("err_rate", 0)
            _healthy  = _hb.get("healthy", True)
            _hb_color = "#4ADE80" if _healthy else "#F87171"
            st.markdown(
                f"<div style='display:flex;gap:12px;margin-bottom:8px;padding:8px 10px;"
                f"background:#18181B;border-radius:8px;border:0.5px solid #27272A'>"
                f"<div style='text-align:center'>"
                f"<div style='font-size:1.1rem;font-weight:600;color:#F4F4F5'>{_hb.get('vm_count',0)}</div>"
                f"<div style='font-size:0.62rem;color:#52525B;font-family:JetBrains Mono'>VMs</div></div>"
                f"<div style='text-align:center'>"
                f"<div style='font-size:1.1rem;font-weight:600;color:#F4F4F5'>{_hb.get('bkt_count',0)}</div>"
                f"<div style='font-size:0.62rem;color:#52525B;font-family:JetBrains Mono'>Buckets</div></div>"
                f"<div style='text-align:center'>"
                f"<div style='font-size:1.1rem;font-weight:600;color:{_hb_color}'>{_err_rate:.1f}</div>"
                f"<div style='font-size:0.62rem;color:#52525B;font-family:JetBrains Mono'>err/min</div></div>"
                f"</div>", unsafe_allow_html=True)
        else:
            st.markdown(
                "<div style='font-size:0.75rem;color:#52525B;padding:6px 0;font-family:JetBrains Mono'>"
                "Monitor agent not connected</div>", unsafe_allow_html=True)

        if _mals:
            _sev_color = {"critical":"#F87171","high":"#FCD34D","medium":"#818CF8","low":"#71717A"}
            for al in _mals[-8:]:
                sev   = al.get("severity","medium")
                color = _sev_color.get(sev,"#71717A")
                ts    = (al.get("ts","") or "")[11:16]
                _fix  = al.get("suggested_action","")
                _fix_html = (
                    f"<div style='font-size:0.62rem;color:#4ADE80;margin-top:2px'>→ {_fix[:60]}</div>"
                    if _fix else ""
                )
                st.markdown(
                    f"<div style='border-left:2px solid {color};padding:6px 10px 6px 8px;"
                    f"background:#18181B;border-radius:0 6px 6px 0;margin-bottom:5px'>"
                    f"<div style='font-size:0.72rem;font-weight:500;color:{color}'>"
                    f"{al.get('title','')} "
                    f"<span style='float:right;color:#3F3F46;font-weight:400;font-family:JetBrains Mono'>{ts}</span></div>"
                    f"<div style='font-size:0.68rem;color:#71717A;margin-top:2px'>{al.get('body','')[:90]}</div>"
                    f"{_fix_html}"
                    f"</div>", unsafe_allow_html=True)

            mc1, mc2 = st.columns(2)
            with mc1:
                if st.button("Clear", key="mon_clear", use_container_width=True):
                    st.session_state.monitor_alerts = []; st.rerun()
            with mc2:
                if st.button("Run now", key="mon_run", use_container_width=True):
                    _trigger_monitor_run(); st.rerun()
        else:
            st.caption("No active alerts — infrastructure healthy.")
            if st.button("Run check now", key="mon_run2", use_container_width=True):
                _trigger_monitor_run(); st.rerun()

    # ═══════════════════════════════════════════════════════════════════════
    # TAB 3 — HISTORY (audit + versions)
    # ═══════════════════════════════════════════════════════════════════════
    with _tab_history:
        # ── Audit trail ───────────────────────────────────────────────────
        with st.expander("Audit trail", expanded=False):
            audit_entries = read_audit_log(max_entries=40)
            if audit_entries:
                _sev_col = {"error":"#F87171","apply":"#4ADE80","plan":"#818CF8","destroy":"#FCD34D"}
                for entry in audit_entries[:15]:
                    action = entry.get("action","")
                    user   = entry.get("user","")
                    ts     = (entry.get("ts","") or entry.get("ended",""))[:16].replace("T"," ")
                    err_col = "#F87171" if action == "error" else "#4ADE80" if action == "apply" else "#3F3F46"
                    added     = entry.get("added",[])
                    destroyed = entry.get("destroyed",[])
                    cost      = (entry.get("cost_estimate") or {}).get("total_monthly",0)
                    details   = []
                    if added:     details.append(f"+{len(added)}")
                    if destroyed: details.append(f"−{len(destroyed)}")
                    if cost:      details.append(f"${cost:.0f}/mo")
                    detail_str = " · ".join(details) or (entry.get("message","")[:60])
                    _dh = ("<div style='font-size:0.65rem;color:#71717A;margin-top:1px'>" + detail_str + "</div>") if detail_str else ""
                    st.markdown(
                        f"<div style='border-left:2px solid {err_col};padding:5px 8px;"
                        f"background:#18181B;border-radius:0 6px 6px 0;margin-bottom:4px'>"
                        f"<div style='font-family:JetBrains Mono;font-size:0.65rem'>"
                        f"<span style='color:{err_col};font-weight:600'>{action.upper()}</span>"
                        f" <span style='color:#52525B'>{user} · {ts}</span></div>"
                        f"{_dh}"
                        f"</div>", unsafe_allow_html=True)
            else:

                st.caption("No audit entries yet.")

        # ── Version history ───────────────────────────────────────────────
        with st.expander("Version history", expanded=False):
            if st.session_state.rollback_job_id:
                _rj = get_job(st.session_state.rollback_job_id)
                if _rj.get("status") == "running":
                    st.info("⏳ Preparing rollback…")
                elif (_rj.get("result") or {}).get("status") == "error":
                    st.error((_rj.get("result") or {}).get("message","Rollback failed."))
                    st.session_state.rollback_job_id = None
                elif _rj.get("status") == "done":
                    st.session_state.rollback_job_id = None; st.rerun()

            versions = get_version_history()
            if not versions:
                st.caption("No versions yet — applied runs are saved here.")
            else:
                for v in versions[:8]:
                    v_id  = v.get("version_id","")[:12]
                    v_ts  = v.get("ts","")[:16].replace("T"," ")
                    v_res = v.get("resource_count", 0)
                    v_act = v.get("action","apply")
                    v_col = "#F87171" if v_act == "destroy" else "#4ADE80"
                    st.markdown(
                        f"<div style='background:#18181B;border:0.5px solid #27272A;"
                        f"border-radius:8px;padding:8px 10px;margin-bottom:5px'>"
                        f"<div style='font-family:JetBrains Mono;font-size:0.68rem;"
                        f"color:#71717A;display:flex;justify-content:space-between'>"
                        f"<span style='color:#A1A1AA'>{v_ts}</span>"
                        f"<span style='color:{v_col}'>{v_act}</span></div>"
                        f"<div style='font-size:0.7rem;color:#F4F4F5;margin-top:3px'>"
                        f"{v_res} resource(s) · <span style='font-family:JetBrains Mono;color:#52525B'>{v_id}</span>"
                        f"</div></div>", unsafe_allow_html=True)
                    if not (st.session_state.ready_to_apply or st.session_state.rollback_job_id):
                        if st.button(f"Roll back to this", key=f"rb_{v.get('version_id','')}",
                                     use_container_width=True):
                            st.session_state.rollback_job_id = start_rollback_job(
                                v, user=get_current_user())
                            st.rerun()

    # ═══════════════════════════════════════════════════════════════════════
    # TAB 4 — DRIFT
    # ═══════════════════════════════════════════════════════════════════════
    with _tab_drift:
        st.markdown(
            "<div style='font-size:0.72rem;color:#52525B;margin-bottom:10px'>"
            "Detect resources modified or deleted outside Terraform.</div>",
            unsafe_allow_html=True)

        if st.session_state.drift_fix_job_id:
            _dfj = get_job(st.session_state.drift_fix_job_id)
            if _dfj.get("status") == "running":
                st.info("⏳ Fixing drift…")
                time.sleep(1.5); st.rerun()
            elif _dfj.get("status") in ("done","error"):
                r = (_dfj.get("result") or {})
                if r.get("status") == "error":
                    st.error(r.get("message","Fix failed."))
                else:
                    st.success(r.get("message","Drift fixed."))
                st.session_state.drift_fix_job_id = None
                st.session_state.drift_result     = None

        _drift_jid = st.session_state.drift_job_id
        if _drift_jid:
            _dj  = get_job(_drift_jid)
            _dst = _dj.get("status","running")
            if _dst == "running":
                st.info("⏳ Scanning for drift…")
                time.sleep(1.5); st.rerun()
            elif _dst in ("done","error"):
                st.session_state.drift_result  = (_dj.get("result") or {}).get("drift")
                st.session_state.drift_job_id  = None
            else:
                st.session_state.drift_job_id = None

        _drift = st.session_state.drift_result
        if _drift:
            if _drift.get("clean"):
                st.markdown(
                    "<div style='background:#052E1A;border:0.5px solid #14532D;border-radius:8px;"
                    "padding:10px 12px;font-size:0.78rem;color:#4ADE80'>✓ No drift — GCP matches Terraform state</div>",
                    unsafe_allow_html=True)
            else:
                st.warning(_drift.get("summary","Drift detected."))
                if _drift.get("how_to_fix"):
                    with st.expander("How to fix", expanded=True):
                        st.markdown(_drift.get("how_to_fix",""))

                _fc1, _fc2, _fc3 = st.columns(3)
                with _fc1:
                    if (st.button("Reconcile state", key="d_reconcile", use_container_width=True)
                            and not st.session_state.ready_to_apply):
                        st.session_state.drift_fix_job_id = start_drift_fix_job(
                            _drift, "reconcile", user=get_current_user())
                        st.rerun()
                with _fc2:
                    if (st.button("Revert to code", key="d_revert", use_container_width=True)
                            and not st.session_state.ready_to_apply):
                        st.session_state.drift_fix_job_id = start_drift_fix_job(
                            _drift, "revert", user=get_current_user())
                        st.rerun()
                with _fc3:
                    if st.button("Clean state", key="d_rm", use_container_width=True):
                        st.session_state.drift_fix_job_id = start_drift_fix_job(
                            _drift, "remove_state", user=get_current_user())
                        st.rerun()

            if st.button("Re-scan", key="drift_rescan", use_container_width=True):
                st.session_state.drift_result    = None
                st.session_state.drift_job_id    = start_drift_job(user=get_current_user())
                st.rerun()
        else:
            if st.button("Scan for drift", key="drift_scan_btn", use_container_width=True):
                st.session_state.drift_job_id = start_drift_job(user=get_current_user())
                st.rerun()

    # ═══════════════════════════════════════════════════════════════════════
    # TAB 5 — DEPLOY (repo URL → analyse → plan → apply)
    # ═══════════════════════════════════════════════════════════════════════
    with _tab_deploy:

        # ── Stage indicator helper ────────────────────────────────────
        def _stage_bar(active: int):
            """Render a 4-stage progress bar. active=1..4"""
            stages  = ["Connect", "Analyse", "Plan", "Apply"]
            icons   = ["🔗", "🤖", "📋", "🚀"]
            colors  = ["#4d9eff", "#a78bfa", "#f59e0b", "#22c55e"]
            parts   = []
            for i, (s, ic, c) in enumerate(zip(stages, icons, colors), 1):
                done  = i < active
                cur   = i == active
                col   = c if (done or cur) else "#30363d"
                dot   = "●" if done else ("◉" if cur else "○")
                bold  = "font-weight:600;" if cur else ""
                parts.append(
                    f"<span style='color:{col};{bold}font-size:0.62rem'>"
                    f"{dot} {ic} {s}</span>")
            html = " <span style='color:#30363d;font-size:0.6rem'>──</span> ".join(parts)
            st.markdown(
                f"<div style='margin:0 0 12px;letter-spacing:0.03em'>{html}</div>",
                unsafe_allow_html=True)

        # ── Poll running deploy job ───────────────────────────────────
        _dep_jid = st.session_state.deploy_job_id
        if _dep_jid:
            _dj   = get_job(_dep_jid)
            _dst  = _dj.get("status", "running")
            _logs = _dj.get("logs", [])

            if _dst == "running":
                # Detect current stage from logs
                _stage = 1
                for lg in _logs:
                    if "Cloning" in lg or "Cloned" in lg:   _stage = max(_stage, 1)
                    if "Scanning" in lg or "Gemini" in lg:  _stage = max(_stage, 2)
                    if "terraform plan" in lg.lower():       _stage = max(_stage, 3)
                _stage_bar(_stage)

                # Live log feed
                st.markdown(
                    "<div style='font-family:JetBrains Mono;font-size:0.62rem;"
                    "background:#0d1117;border:0.5px solid #21262d;border-radius:6px;"
                    "padding:8px 10px;max-height:140px;overflow-y:auto'>",
                    unsafe_allow_html=True)
                for lg in _logs[-8:]:
                    icon = "✅" if lg.startswith("✅") else \
                           "❌" if lg.startswith("❌") else \
                           "⚠️" if lg.startswith("⚠️") else "·"
                    col  = "#22c55e" if "✅" in icon else \
                           "#f85149" if "❌" in icon else \
                           "#f59e0b" if "⚠️" in icon else "#8b949e"
                    safe = lg.replace("<","&lt;").replace(">","&gt;")
                    st.markdown(
                        f"<div style='color:{col};margin-bottom:1px'>{safe}</div>",
                        unsafe_allow_html=True)
                st.markdown("</div>", unsafe_allow_html=True)

                if st.button("✕ Cancel", key="deploy_cancel",
                             use_container_width=True):
                    st.session_state.deploy_job_id = None
                    TerraformTools.revert_to_snapshot()
                    st.rerun()

                time.sleep(1.5); st.rerun()

            elif _dst in ("done", "error"):
                _dr   = (_dj.get("result") or {})
                _meta = _dr.get("deploy_meta", {})

                if _dr.get("status") == "error" or _dst == "error":
                    _stage_bar(2)
                    _emsg = _dr.get("message", "Deploy analysis failed.")
                    st.markdown(
                        f"<div style='background:#1a0505;border:0.5px solid #f85149;"
                        f"border-radius:8px;padding:12px 14px;font-size:0.8rem;"
                        f"color:#f85149;margin-bottom:10px'>"
                        f"<div style='font-weight:600;margin-bottom:4px'>❌ Deploy failed</div>"
                        f"<div style='font-size:0.72rem;line-height:1.6'>{_emsg}</div>"
                        f"</div>",
                        unsafe_allow_html=True)
                    if st.button("↩ Try again", key="deploy_retry",
                                 use_container_width=True):
                        st.session_state.deploy_job_id = None
                        st.rerun()

                else:
                    _stage_bar(3)

                    # ── Success summary card ──────────────────────────
                    _app   = _meta.get("app_name","")
                    _env   = _meta.get("environment","production")
                    _type  = _meta.get("project_type","app")
                    _repo  = (_meta.get("repo_url","").split("/")[-1]
                              .replace(".git",""))
                    _adds  = len(_dr.get("plan_details",{}).get("add",[]))
                    _cost  = (_dr.get("cost_estimate") or {}).get("total_monthly",0)
                    _secs  = _dr.get("security_audit") or []
                    _high  = sum(1 for f in _secs if f.get("severity")=="HIGH")

                    st.markdown(
                        f"<div style='background:#0d1117;border:0.5px solid #21262d;"
                        f"border-left:3px solid #22c55e;border-radius:0 8px 8px 0;"
                        f"padding:12px 14px;margin-bottom:10px'>"
                        f"<div style='font-size:0.68rem;color:#22c55e;font-weight:600;"
                        f"margin-bottom:6px'>✅ PLAN READY</div>"
                        f"<div style='font-size:0.82rem;color:#c9d1d9;font-weight:600;"
                        f"margin-bottom:8px'>{_app or _repo}</div>"
                        f"<div style='display:flex;gap:14px;font-size:0.68rem;color:#8b949e'>"
                        f"<span>📦 {_type}</span>"
                        f"<span>🌍 {_env}</span>"
                        f"<span>➕ {_adds} resource(s)</span>"
                        f"{'<span>💰 $'+str(round(_cost,2))+'/mo</span>' if _cost else ''}"
                        f"{'<span style=color:#f59e0b>⚠️ '+str(_high)+' HIGH sec</span>' if _high else ''}"
                        f"</div>"
                        f"</div>",
                        unsafe_allow_html=True)

                    # Dockerfile push hint
                    if _meta.get("has_dockerfile"):
                        _img = _meta.get("image_hint","gcr.io/PROJECT/APP:latest")
                        st.markdown(
                            f"<div style='background:#130d00;border:0.5px solid #f59e0b;"
                            f"border-radius:8px;padding:10px 12px;font-size:0.72rem;"
                            f"color:#f59e0b;margin-bottom:10px'>"
                            f"<div style='font-weight:600;margin-bottom:4px'>"
                            f"⚠️ Push Docker image first</div>"
                            f"<div style='font-family:JetBrains Mono;font-size:0.62rem;"
                            f"color:#d4a017;margin-top:4px;white-space:pre-wrap'>"
                            f"gcloud builds submit --tag {_img} .</div>"
                            f"</div>",
                            unsafe_allow_html=True)

                    # sre.yaml hint if not present
                    if not _meta.get("has_sre_yaml"):
                        st.markdown(
                            "<div style='background:#0a0d13;border:0.5px solid #1d4ed8;"
                            "border-radius:8px;padding:8px 12px;font-size:0.68rem;"
                            "color:#60a5fa;margin-bottom:10px'>"
                            "💡 Add <code>sre.yaml</code> to your repo root to control "
                            "memory, CPU, environment triggers, and secrets.</div>",
                            unsafe_allow_html=True)

                    # Push plan to chat + set ready_to_apply
                    _dc1, _dc2 = st.columns([3, 2])
                    with _dc1:
                        if st.button("🚀 Apply Deployment", key="deploy_apply_btn",
                                     use_container_width=True,
                                     disabled=st.session_state.thinking):
                            st.session_state.messages.append({
                                "role": "user",
                                "content": f"🚀 Deploy `{_app or _repo}` to {_env}"
                            })
                            st.session_state.messages.append({
                                "role":    "assistant",
                                "type":    "success",
                                "content": _dr.get("message",""),
                                "action":  "apply",
                                "plan_details":   _dr.get("plan_details",{}),
                                "is_destroy":     False,
                                "has_warn":       False,
                                "pending_count":  _adds,
                                "cost_estimate":  _dr.get("cost_estimate"),
                                "security_audit": _secs,
                                "auto_fixed":     [],
                                "is_rollback":    False,
                                "rollback_version": None,
                            })
                            st.session_state.ready_to_apply   = True
                            st.session_state.plan_ts          = time.time()
                            st.session_state.is_destroy       = False
                            st.session_state["_last_action"]         = "apply"
                            st.session_state["_last_action_idx"]     = \
                                len(st.session_state.messages) - 1
                            st.session_state["_last_action_pcount"]  = _adds
                            st.session_state.deploy_job_id = None
                            # Save to deploy history
                            if "deploy_history" not in st.session_state:
                                st.session_state.deploy_history = []
                            st.session_state.deploy_history.insert(0, {
                                "repo":    _repo,
                                "app":     _app,
                                "env":     _env,
                                "type":    _type,
                                "ts":      datetime.utcnow().strftime("%H:%M %d/%m"),
                                "status":  "applying",
                            })
                            st.rerun()
                    with _dc2:
                        if st.button("✕ Discard", key="deploy_discard",
                                     use_container_width=True):
                            TerraformTools.revert_to_snapshot()
                            st.session_state.deploy_job_id = None
                            st.rerun()

        # ── Deploy Form (idle state) ──────────────────────────────────
        if not _dep_jid:

            # ── Repo URL — primary input ──────────────────────────────
            _url_val = st.session_state.deploy_repo_url
            _new_url = st.text_input(
                "Git repository URL",
                value=_url_val,
                placeholder="https://github.com/owner/my-app",
                key="deploy_url_input")
            st.session_state.deploy_repo_url = _new_url

            # URL validation hint
            _url_ok = _new_url.strip().startswith("https://") or \
                      _new_url.strip().startswith("http://")
            if _new_url.strip() and not _url_ok:
                st.markdown(
                    "<div style='font-size:0.65rem;color:#f85149;margin:-6px 0 6px'>"
                    "⚠️ Must start with https://</div>",
                    unsafe_allow_html=True)

            # ── Prompt — the key differentiator ──────────────────────
            _new_prompt = st.text_area(
                "What do you want to deploy?",
                value=st.session_state.deploy_prompt,
                placeholder=(
                    "e.g. Deploy this FastAPI app to Cloud Run\n"
                    "     with 512MB RAM, 2 CPUs, public HTTPS access\n\n"
                    "e.g. Deploy as a private staging service with 0 min instances\n\n"
                    "e.g. This is a Next.js static site, deploy to GCS with CDN"),
                height=100,
                key="deploy_prompt_input")
            st.session_state.deploy_prompt = _new_prompt

            # ── Config row ────────────────────────────────────────────
            _c1, _c2, _c3 = st.columns(3)
            with _c1:
                _envs = ["production", "staging", "dev"]
                _ei   = _envs.index(st.session_state.deploy_env) \
                        if st.session_state.deploy_env in _envs else 0
                st.session_state.deploy_env = st.selectbox(
                    "Environment", _envs, index=_ei, key="dep_env")
            with _c2:
                st.session_state.deploy_branch = st.text_input(
                    "Branch",
                    value=st.session_state.deploy_branch or "main",
                    placeholder="main", key="dep_branch")
            with _c3:
                st.session_state.deploy_app_name = st.text_input(
                    "App name",
                    value=st.session_state.deploy_app_name,
                    placeholder="auto",
                    key="dep_name",
                    help="Leave blank to auto-detect from repo name")

            # ── Private repo token ────────────────────────────────────
            with st.expander("🔑 Private repository", expanded=False):
                st.session_state.deploy_token = st.text_input(
                    "Access token",
                    value=st.session_state.deploy_token,
                    type="password",
                    placeholder="ghp_... (GitHub PAT) or GitLab deploy token",
                    key="dep_token")
                st.markdown(
                    "<div style='font-size:0.62rem;color:#484f58;margin-top:4px'>"
                    "Used only to clone — never stored after this session.</div>",
                    unsafe_allow_html=True)

            # ── Deploy button ─────────────────────────────────────────
            _ready = _url_ok and _new_url.strip()
            st.markdown("<div style='margin-top:6px'>", unsafe_allow_html=True)
            if st.button(
                "🚀 Analyse & Plan",
                key="deploy_btn",
                use_container_width=True,
                disabled=not _ready or bool(st.session_state.thinking)):

                _url    = _new_url.strip()
                _branch = st.session_state.deploy_branch.strip() or "main"
                _name   = st.session_state.deploy_app_name.strip()
                _env    = st.session_state.deploy_env
                _prompt = st.session_state.deploy_prompt.strip()
                _token  = st.session_state.deploy_token.strip()

                st.session_state.deploy_job_id = start_deploy_job(
                    repo_url    = _url,
                    branch      = _branch,
                    app_name    = _name,
                    environment = _env,
                    user_prompt = _prompt,
                    repo_token  = _token,
                    user        = get_current_user(),
                )
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

            # ── What happens hint ─────────────────────────────────────
            st.markdown(
                "<div style='font-size:0.6rem;color:#30363d;margin-top:10px;"
                "line-height:1.9;border-top:0.5px solid #21262d;padding-top:8px'>"
                "🔗 Clone repo &nbsp;→&nbsp; "
                "🤖 Gemini analyses stack &nbsp;→&nbsp; "
                "📋 Terraform plan + cost &nbsp;→&nbsp; "
                "🚀 One-click apply"
                "</div>",
                unsafe_allow_html=True)

            # ── Deploy history ────────────────────────────────────────
            _hist = st.session_state.get("deploy_history", [])
            if _hist:
                st.markdown(
                    "<div style='font-size:0.68rem;font-weight:600;color:#484f58;"
                    "margin:14px 0 6px'>Recent deployments</div>",
                    unsafe_allow_html=True)
                for _rec in _hist[:5]:
                    _s   = _rec.get("status","done")
                    _sc  = "#22c55e" if _s=="done" else \
                           "#f59e0b" if _s=="applying" else "#8b949e"
                    _dot = "●"
                    st.markdown(
                        f"<div style='display:flex;align-items:center;gap:8px;"
                        f"font-size:0.68rem;color:#8b949e;padding:3px 0;"
                        f"border-bottom:0.5px solid #21262d'>"
                        f"<span style='color:{_sc}'>{_dot}</span>"
                        f"<span style='color:#c9d1d9;font-weight:500'>"
                        f"{_rec.get('app') or _rec.get('repo','?')}</span>"
                        f"<span style='color:#484f58'>{_rec.get('env','')}</span>"
                        f"<span style='margin-left:auto'>{_rec.get('ts','')}</span>"
                        f"</div>",
                        unsafe_allow_html=True)

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
            <div style='display:flex;flex-direction:column;align-items:center;
                        justify-content:center;height:100%;padding:3rem 2rem;gap:0'>
              <div style='width:48px;height:48px;background:#1E1B4B;border-radius:14px;
                          display:flex;align-items:center;justify-content:center;
                          font-size:22px;margin-bottom:16px'>⬡</div>
              <p style='color:#F4F4F5;font-weight:600;font-size:1rem;margin:0 0 6px'>
                What do you want to build?
              </p>
              <p style='font-size:0.8rem;color:#52525B;margin:0 0 24px;text-align:center;max-width:320px'>
                Describe any GCP infrastructure — I'll write the Terraform,
                validate it and show you the plan before applying.
              </p>
              <div style='display:flex;flex-wrap:wrap;gap:8px;justify-content:center;max-width:420px'>
                <div style='background:#18181B;border:0.5px solid #3F3F46;border-radius:8px;
                            padding:7px 13px;font-size:0.78rem;color:#A1A1AA;cursor:default'>
                  GCS bucket in europe-west2</div>
                <div style='background:#18181B;border:0.5px solid #3F3F46;border-radius:8px;
                            padding:7px 13px;font-size:0.78rem;color:#A1A1AA;cursor:default'>
                  VM in us-central1 ubuntu 24</div>
                <div style='background:#18181B;border:0.5px solid #3F3F46;border-radius:8px;
                            padding:7px 13px;font-size:0.78rem;color:#A1A1AA;cursor:default'>
                  GKE cluster + node pool</div>
                <div style='background:#18181B;border:0.5px solid #3F3F46;border-radius:8px;
                            padding:7px 13px;font-size:0.78rem;color:#A1A1AA;cursor:default'>
                  List all running VMs</div>
                <div style='background:#18181B;border:0.5px solid #3F3F46;border-radius:8px;
                            padding:7px 13px;font-size:0.78rem;color:#A1A1AA;cursor:default'>
                  Check infrastructure drift</div>
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
                fix_label = f"Fix {len(_fixable_ids)} security issue(s)"
                if st.button(fix_label, key="action_fix_security", use_container_width=True):
                    if require_admin("fixing security issues"):
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
                label = f"Apply  ({_pcount} new)" if _pcount else "Apply changes"
                if st.session_state.is_rollback:
                    rv = st.session_state.rollback_version or {}
                    label = f"Confirm rollback  {rv.get('version_id','')[:12]}"
                if st.button(label, key="action_apply", use_container_width=True):
                    if require_admin("applying infrastructure changes"):
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
                if st.button("Discard plan", key="action_discard_apply",
                             use_container_width=True):
                    _discard_plan(_action_idx)
                    st.rerun()

        elif _action == "destroy":
            with btn_cols[0]:
                st.markdown('<div class="btn-destroy">', unsafe_allow_html=True)
                if st.button("Confirm destroy", key="action_destroy", use_container_width=True):
                    if require_admin("destroying infrastructure"):
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
                if st.button("Cancel", key="action_cancel_d", use_container_width=True):
                    _discard_plan(_action_idx)
                    st.rerun()

        elif _action == "warn_destroy":
            with btn_cols[0]:
                st.markdown('<div class="btn-destroy">', unsafe_allow_html=True)
                if st.button("Apply anyway", key="action_warn", use_container_width=True):
                    if require_admin("applying destructive changes"):
                        st.session_state.apply_job_id   = start_apply_job(user=get_current_user())
                        st.session_state.ready_to_apply = False
                        st.session_state.plan_ts        = None
                        st.session_state["_last_action"]= None
                        if _action_idx < len(st.session_state.messages):
                            st.session_state.messages[_action_idx].pop("action", None)
                        st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
            with btn_cols[1]:
                if st.button("Cancel", key="action_cancel_w", use_container_width=True):
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
            elif r == "drift":
                # Drift result from agent — populate the drift panel
                drift = result.get("drift") or {}
                st.session_state.drift_result = drift
                st.session_state.drift_job_id = None
                # Also add a chat bubble summarising the result
                summary = drift.get("summary", "Drift check complete.")
                how_to  = drift.get("how_to_fix", "")
                content = summary
                if how_to and not drift.get("clean"):
                    content += f"\n\n{how_to}"
                st.session_state.messages.append({
                    "role":    "assistant",
                    "type":    "info",
                    "content": content,
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
            if st.button("Refresh", use_container_width=True):
                st.rerun()

    # ── Tab: Voice ────────────────────────────────────────────────────────────
    with tab_voice:
        # ── Model selector ────────────────────────────────────────────────
        _VOICE_MODELS = ["gemini-2.5-flash-preview-04-17", "gemini-2.0-flash-lite", "gemini-2.5-pro-preview-03-25"]
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
            if st.button("Transcribe", key="voice_transcribe",
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
                if st.button("Send to agent", key="voice_send",
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
                if st.button("Discard", key="voice_clear",
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
