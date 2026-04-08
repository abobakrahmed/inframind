"""
AI SRE Agent — Auth Service (aiohttp, single port)
One process, one port ($PORT). aiohttp handles HTTP + WebSocket natively.
WS upgrade is detected and proxied via aiohttp client ws_connect.
"""
import os, json, time, hashlib, hmac, base64, logging, asyncio
from typing import Optional
from datetime import datetime, timezone
from collections import defaultdict
import threading
import urllib.request as _ur
import urllib.parse as _up

import aiohttp
from aiohttp import web, WSMsgType

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY        = os.environ.get("AUTH_SECRET_KEY", "change-me!")
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "8"))
STREAMLIT_URL     = os.environ.get("STREAMLIT_URL", "http://ai-sre-gcp:8080")
INTERNAL_SECRET   = os.environ.get("INTERNAL_SECRET", "")
AUTH_REALM        = os.environ.get("AUTH_REALM", "AI SRE Agent")
COOKIE_NAME       = "sre_session"
COOKIE_SECURE     = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_ATTEMPTS      = int(os.environ.get("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_SECS      = int(os.environ.get("LOCKOUT_SECONDS", "300"))
PORT              = int(os.environ.get("PORT", "8080"))

if STREAMLIT_URL.startswith("http://") and ".run.app" in STREAMLIT_URL:
    STREAMLIT_URL = STREAMLIT_URL.replace("http://", "https://", 1)
STREAMLIT_WS = STREAMLIT_URL.replace("http://", "ws://").replace("https://", "wss://")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auth")

# ── GCP Identity Token (service-to-service auth) ──────────────────────────────
# Cloud Run services with --no-allow-unauthenticated require every caller
# to present a valid GCP OIDC identity token in Authorization: Bearer <token>.
# We fetch it from the GCP metadata server (available inside Cloud Run).
_id_token_cache = {"token": "", "exp": 0}
_id_token_lock  = threading.Lock()

def _get_identity_token(audience: str) -> str:
    """
    Fetch a GCP OIDC identity token for calling another Cloud Run service.
    Uses the metadata server — works automatically inside Cloud Run.
    Cached for 55 minutes (tokens last 1 hour).
    Returns empty string if not running on GCP (e.g. local dev).
    """
    now = time.time()
    with _id_token_lock:
        if _id_token_cache["token"] and now < _id_token_cache["exp"]:
            return _id_token_cache["token"]
    try:
        meta_url = (
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={_up.quote(audience, safe='')}"
        )
        req  = _ur.Request(meta_url, headers={"Metadata-Flavor": "Google"})
        resp = _ur.urlopen(req, timeout=5)
        token = resp.read().decode().strip()
        with _id_token_lock:
            _id_token_cache["token"] = token
            _id_token_cache["exp"]   = now + 55 * 60  # cache 55 min
        log.debug("Identity token refreshed")
        return token
    except Exception as exc:
        log.warning(f"Identity token fetch failed (not on GCP?): {exc}")
        return ""

# ── Brute-force ───────────────────────────────────────────────────────────────
_fail: dict = defaultdict(lambda: {"n": 0, "t": 0.0})
_fl = threading.Lock()

def _locked(ip):
    with _fl:
        r = _fail[ip]
        if r["n"] >= MAX_ATTEMPTS:
            if time.time() - r["t"] < LOCKOUT_SECS: return True
            _fail[ip] = {"n": 0, "t": 0.0}
    return False

def _inc(ip):
    with _fl:
        r = _fail[ip]
        if r["n"] == 0: r["t"] = time.time()
        r["n"] += 1

def _clr(ip):
    with _fl: _fail[ip] = {"n": 0, "t": 0.0}

# ── JWT ───────────────────────────────────────────────────────────────────────
_b64e = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
_b64d = lambda s: base64.urlsafe_b64decode(s + "=" * (4 - len(s) % 4))

def make_token(user, role):
    h = _b64e(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    n = int(time.time())
    p = _b64e(json.dumps({"sub":user,"role":role,"iat":n,"exp":n+SESSION_TTL_HOURS*3600}).encode())
    s = hmac.new(SECRET_KEY.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64e(s)}"

def check_token(tok):
    try:
        h, p, s = tok.split(".")
        ok = _b64e(hmac.new(SECRET_KEY.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(s, ok): return None
        c = json.loads(_b64d(p))
        return c if c["exp"] > time.time() else None
    except: return None

def claims(req): 
    t = req.cookies.get(COOKIE_NAME)
    return check_token(t) if t else None

# ── Users ─────────────────────────────────────────────────────────────────────
def load_users():
    if os.environ.get("AUTH_USERS"):
        try: return json.loads(os.environ["AUTH_USERS"])
        except: pass
    if os.environ.get("ADMIN_PASSWORD"):
        return [{"username":"admin","password":os.environ["ADMIN_PASSWORD"],"role":"admin"}]
    if os.path.exists("/app/users.json"):
        return json.load(open("/app/users.json"))
    return [{"username":"admin","password":"changeme123","role":"admin"}]

def auth(u, p):
    for usr in load_users():
        if usr["username"].lower() != u.lower(): continue
        s = usr["password"]
        if s == p or s == hashlib.sha256(p.encode()).hexdigest(): return usr
    return None

# ── Login HTML ────────────────────────────────────────────────────────────────
def login_html(err=""):
    e = f'<div class="error">{err}</div>' if err else ""
    return f"""<!DOCTYPE html><html><head><meta charset=UTF-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{AUTH_REALM}</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#08090d;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:'Space Mono',monospace}}
.card{{background:#0f1117;border:1px solid #1e2235;border-radius:16px;padding:40px 48px 48px;width:420px}}
label{{display:block;color:#5a6180;font-size:.7rem;text-transform:uppercase;margin:20px 0 6px}}
input{{width:100%;background:#141720;border:1px solid #1e2235;border-radius:8px;padding:10px 14px;color:#e8eaf0;font-family:inherit;font-size:.85rem;outline:none}}
input:focus{{border-color:#4d9eff}}
.btn{{display:block;width:100%;margin-top:28px;background:linear-gradient(135deg,#00e5a0,#4d9eff);border:none;border-radius:8px;padding:12px;color:#08090d;font-family:inherit;font-weight:700;cursor:pointer}}
.error{{background:rgba(255,77,109,.1);border:1px solid rgba(255,77,109,.3);border-radius:6px;padding:10px 14px;color:#ff4d6d;font-size:.75rem;margin-top:16px}}
h2{{color:#fff;margin-bottom:4px;font-size:1.3rem}}
p{{color:#5a6180;font-size:.7rem;margin-bottom:20px}}</style></head>
<body><div class="card"><h2>⚙️ {AUTH_REALM}</h2><p>// secure access required</p>
<form method=POST action=/login>
<label>Username</label><input name=username type=text autofocus required>
<label>Password</label><input name=password type=password required>
{e}<button class=btn type=submit>AUTHENTICATE →</button>
</form></div></body></html>"""

# ── Hop headers ───────────────────────────────────────────────────────────────
_HOP = {"connection","keep-alive","proxy-authenticate","proxy-authorization",
        "te","trailers","transfer-encoding","upgrade","host",
        "content-encoding","content-length"}
_BYPASS = ("/_stcore/","/static/","/vendor/","/favicon.")

# ── Routes ────────────────────────────────────────────────────────────────────
routes = web.RouteTableDef()

@routes.get("/healthz")
async def healthz(r): 
    return web.json_response({"ok":True,"t":datetime.now(timezone.utc).isoformat()})

@routes.get("/login")
async def get_login(r): 
    return web.Response(text=login_html(), content_type="text/html")

@routes.post("/login")
async def post_login(req):
    ip = req.remote or "?"
    if _locked(ip):
        return web.Response(text=login_html(f"Too many attempts. Wait {LOCKOUT_SECS//60}m."),
                            content_type="text/html", status=429)
    d    = await req.post()
    user = auth(d.get("username",""), d.get("password",""))
    if not user:
        _inc(ip)
        log.warning(f"AUTH FAIL user={d.get('username')!r} ip={ip}")
        return web.Response(text=login_html("Invalid username or password."),
                            content_type="text/html", status=401)
    _clr(ip)
    token = make_token(user["username"], user.get("role","viewer"))
    nxt   = req.rel_url.query.get("next", "/")
    log.info(f"AUTH OK user={user['username']!r} ip={ip}")
    resp  = web.HTTPFound(nxt)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE,
                    samesite="lax", max_age=SESSION_TTL_HOURS*3600, path="/")
    raise resp

@routes.get("/logout")
async def logout(req):
    r = web.HTTPFound("/login")
    r.del_cookie(COOKIE_NAME, path="/")
    raise r

# ── WebSocket proxy ───────────────────────────────────────────────────────────
@routes.get("/_stcore/stream")
async def ws_proxy(req):
    # JWT check
    c = claims(req)
    if not c:
        log.warning(f"WS AUTH FAIL ip={req.remote} cookie={bool(req.cookies.get(COOKIE_NAME))}")
        raise web.HTTPUnauthorized()

    log.info(f"WS CONNECT user={c['sub']!r} ip={req.remote}")

    # Build upstream target
    qs    = req.query_string
    tgt   = STREAMLIT_WS + "/_stcore/stream"
    parts = [qs] if qs else []
    if INTERNAL_SECRET:
        parts.append(f"_int={INTERNAL_SECRET}")
    if parts:
        tgt += "?" + "&".join(parts)

    log.info(f"WS upstream target: {tgt.split('?')[0]}")

    # Headers to forward upstream
    up_hdrs = {
        "X-Authenticated-User": c["sub"],
        "X-User-Role":          c.get("role", "viewer"),
    }
    if INTERNAL_SECRET:
        up_hdrs["X-Internal-Secret"] = INTERNAL_SECRET

    # GCP identity token for service-to-service auth
    if ".run.app" in STREAMLIT_WS:
        id_tok = _get_identity_token(STREAMLIT_URL)
        if id_tok:
            up_hdrs["Authorization"] = f"Bearer {id_tok}"

    # Accept browser WebSocket FIRST — before connecting upstream
    # (browser times out if we take too long to accept)
    # Extract protocols BEFORE accepting browser WS
    proto_header = req.headers.get("sec-websocket-protocol", "")
    protocols    = [p.strip() for p in proto_header.split(",") if p.strip()]
    log.info(f"WS protocols={protocols}")

    # Accept with protocols so browser sees negotiated subprotocol
    ws_in = web.WebSocketResponse(
        heartbeat = 25,
        compress  = False,
        protocols = protocols,    # ← fixes "don't overlap" warning
    )
    await ws_in.prepare(req)
    log.info(f"WS browser accepted user={c['sub']!r} proto={ws_in.ws_protocol!r}")

    sess = req.app["sess"]
    try:
        async with sess.ws_connect(
            tgt,
            headers      = up_hdrs,
            ssl          = None,   # None = use default SSL (trusts GCP certs on *.run.app)
            heartbeat    = 25,
            max_msg_size = 10 * 1024 * 1024,
            protocols    = protocols if protocols else None,
            timeout      = aiohttp.ClientTimeout(total=None, connect=10),
        ) as ws_out:
            log.info(f"WS upstream connected user={c['sub']!r} proto={ws_out.protocol!r}")

            async def browser_to_upstream():
                try:
                    async for msg in ws_in:
                        if msg.type == WSMsgType.TEXT:
                            await ws_out.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_out.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break
                except Exception as e:
                    log.debug(f"WS b->u: {e}")

            async def upstream_to_browser():
                try:
                    async for msg in ws_out:
                        if msg.type == WSMsgType.TEXT:
                            await ws_in.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws_in.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break
                except Exception as e:
                    log.debug(f"WS u->b: {e}")

            done, pending = await asyncio.wait(
                [asyncio.ensure_future(browser_to_upstream()),
                 asyncio.ensure_future(upstream_to_browser())],
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()

    except aiohttp.ClientConnectorError as exc:
        log.error(f"WS upstream UNREACHABLE: {exc} → {tgt.split('?')[0]}")
    except aiohttp.WSServerHandshakeError as exc:
        log.error(f"WS upstream HANDSHAKE FAILED status={exc.status}: {exc}")
    except Exception as exc:
        log.error(f"WS FAILED {type(exc).__name__}: {exc}")

    try:
        await ws_in.close()
    except Exception:
        pass
    log.info(f"WS CLOSE user={c['sub']!r}")
    return ws_in

# ── HTTP proxy — everything else ──────────────────────────────────────────────
@routes.route("*", "/{p:.*}")
async def http_proxy(req):
    path  = "/" + req.match_info.get("p","")
    needs = not any(path.startswith(x) for x in _BYPASS)
    extra = {}
    if needs:
        c = claims(req)
        if not c:
            if req.method == "GET": raise web.HTTPFound(f"/login?next={path}")
            raise web.HTTPUnauthorized()
        extra = {"X-Authenticated-User": c["sub"], "X-User-Role": c.get("role","viewer")}

    tgt = STREAMLIT_URL + path
    if req.query_string: tgt += f"?{req.query_string}"

    fwd = {k:v for k,v in req.headers.items() if k.lower() not in _HOP}
    fwd.update(extra)
    fwd["X-Forwarded-For"]   = req.remote or ""
    fwd["X-Forwarded-Proto"] = req.scheme
    # Force HTTP/1.1 — Streamlit does not support HTTP/2
    fwd["Connection"] = "close"
    if INTERNAL_SECRET: fwd["X-Internal-Secret"] = INTERNAL_SECRET

    # Add GCP identity token so Cloud Run accepts the request
    # (required when app service has --no-allow-unauthenticated)
    if ".run.app" in STREAMLIT_URL:
        id_tok = _get_identity_token(STREAMLIT_URL)
        if id_tok:
            fwd["Authorization"] = f"Bearer {id_tok}"
        else:
            log.warning("No identity token — upstream may return 403")

    try:
        # Create a fresh session per request with force_close=True.
        # This prevents stale-connection "protocol error" on Cloud Run
        # without passing connector= to request() (which is not supported).
        conn = aiohttp.TCPConnector(ssl=None, force_close=True, limit=0)
        async with aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(total=300, connect=10),
            connector_owner=True,
        ) as s:
            async with s.request(
                req.method, tgt,
                headers=fwd,
                data=await req.read(),
                allow_redirects=False,
            ) as r:
                hdr = {k:v for k,v in r.headers.items() if k.lower() not in _HOP}
                return web.Response(body=await r.read(), status=r.status, headers=hdr)
    except aiohttp.ClientConnectorError as exc:
        log.warning(f"Upstream unreachable: {exc}")
        return web.Response(
            text="<h3 style='font-family:monospace;color:#f85149;padding:40px'>"
                 "⚠️ SRE Agent starting — retry in a few seconds</h3>",
            content_type="text/html", status=503)
    except aiohttp.ServerDisconnectedError as exc:
        log.warning(f"Upstream disconnected: {exc}")
        return web.Response(text="Service temporarily unavailable — please refresh.",
                            status=503)
    except Exception as exc:
        log.error(f"HTTP proxy error: {type(exc).__name__}: {exc}")
        return web.Response(text=f"Gateway error: {exc}", status=502)

# ── App startup/shutdown ──────────────────────────────────────────────────────
async def on_startup(app):
    # Validate STREAMLIT_URL — must be the Cloud Run service URL on GCP
    if "ai-sre-gcp:8080" in STREAMLIT_URL or "localhost" in STREAMLIT_URL:
        log.warning(
            f"⚠️  STREAMLIT_URL={STREAMLIT_URL!r} looks like a local/Docker address. "
            "On Cloud Run this must be the ai-sre-agent service URL "
            "(e.g. https://ai-sre-agent-xxx-uc.a.run.app). "
            "Set STREAMLIT_URL env var in Cloud Run to fix."
        )

    # Shared session only for WebSocket upstream connections.
    # HTTP proxy uses per-request connectors (force_close=True) to avoid
    # stale connection reuse which causes "protocol error" on Cloud Run.
    conn = aiohttp.TCPConnector(
        ssl=None,                  # trust GCP-signed certs on *.run.app
        force_close=True,          # never reuse TCP connections
        limit=100,
        enable_cleanup_closed=True,
    )
    app["sess"] = aiohttp.ClientSession(
        connector=conn,
        # No default timeout — WS connections are long-lived
        timeout=aiohttp.ClientTimeout(total=None, connect=10),
    )
    log.info(f"Started on :{PORT} → {STREAMLIT_URL}")

async def on_shutdown(app):
    await app["sess"].close()

app = web.Application(client_max_size=50*1024*1024)
app.add_routes(routes)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT,
                access_log=log,
                keepalive_timeout=75)   # slightly > Cloud Run's 60s idle limit
