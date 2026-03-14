"""
AI SRE Agent — Auth Service
FastAPI service: login/logout + JWT cookie + reverse proxy to Streamlit.
Handles both HTTP and WebSocket (/_stcore/stream) correctly.
"""

import os, json, time, hashlib, hmac, base64, logging, asyncio
from typing import Optional
from datetime import datetime, timezone
from collections import defaultdict

import httpx
import websockets
from fastapi import FastAPI, Request, Response, Form, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY        = os.environ.get("AUTH_SECRET_KEY", "change-me-in-production-32-chars!")
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "8"))
STREAMLIT_URL     = os.environ.get("STREAMLIT_URL", "http://ai-sre-gcp:8080")
STREAMLIT_WS_URL  = STREAMLIT_URL.replace("http://", "ws://").replace("https://", "wss://")
AUTH_REALM        = os.environ.get("AUTH_REALM", "AI SRE Agent")
COOKIE_NAME       = "sre_session"
COOKIE_SECURE     = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO").upper()
MAX_LOGIN_ATTEMPTS= int(os.environ.get("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_SECONDS   = int(os.environ.get("LOCKOUT_SECONDS", "300"))  # 5 min

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("auth")

# ── Brute-force protection ────────────────────────────────────────────────────
# Track failed attempts per IP: {ip: {"count": int, "first_fail": float}}
_fail_tracker: dict = defaultdict(lambda: {"count": 0, "first_fail": 0.0})
_fail_lock = __import__("threading").Lock()

def _is_locked_out(ip: str) -> bool:
    with _fail_lock:
        rec = _fail_tracker[ip]
        if rec["count"] >= MAX_LOGIN_ATTEMPTS:
            if time.time() - rec["first_fail"] < LOCKOUT_SECONDS:
                return True
            else:
                # Lockout expired — reset
                _fail_tracker[ip] = {"count": 0, "first_fail": 0.0}
    return False

def _record_failure(ip: str):
    with _fail_lock:
        rec = _fail_tracker[ip]
        if rec["count"] == 0:
            rec["first_fail"] = time.time()
        rec["count"] += 1

def _clear_failures(ip: str):
    with _fail_lock:
        _fail_tracker[ip] = {"count": 0, "first_fail": 0.0}

# ── Active session tracking ───────────────────────────────────────────────────
# {token_sig: {"user": str, "ip": str, "login_at": str, "last_seen": float}}
_active_sessions: dict = {}
_sessions_lock = __import__("threading").Lock()

def _register_session(sig: str, user: str, ip: str):
    with _sessions_lock:
        _active_sessions[sig] = {
            "user": user, "ip": ip,
            "login_at": datetime.now(timezone.utc).isoformat(),
            "last_seen": time.time(),
        }

def _touch_session(sig: str):
    with _sessions_lock:
        if sig in _active_sessions:
            _active_sessions[sig]["last_seen"] = time.time()

def _revoke_session(sig: str):
    with _sessions_lock:
        _active_sessions.pop(sig, None)

def _get_active_sessions() -> list:
    with _sessions_lock:
        return list(_active_sessions.values())

# ── Users ─────────────────────────────────────────────────────────────────────
def _load_users() -> list[dict]:
    users_file = os.environ.get("USERS_FILE", "/app/users.json")
    if os.path.exists(users_file):
        with open(users_file) as f:
            return json.load(f)
    raw = os.environ.get("AUTH_USERS", "")
    if raw:
        return json.loads(raw)
    admin_pass = os.environ.get("ADMIN_PASSWORD", "admin")
    log.warning("⚠️  Using fallback admin — set AUTH_USERS or ADMIN_PASSWORD")
    return [{"username": "admin", "password": admin_pass, "role": "admin"}]

def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def authenticate(username: str, password: str) -> Optional[dict]:
    for user in _load_users():
        if user["username"].lower() != username.lower():
            continue
        stored = user["password"]
        if stored == password or stored == _hash(password):
            return user
    return None

# ── JWT ───────────────────────────────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (4 - len(s) % 4))

def create_token(username: str, role: str) -> str:
    header  = _b64url_encode(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    now     = int(time.time())
    payload = _b64url_encode(json.dumps({
        "sub": username, "role": role,
        "iat": now, "exp": now + SESSION_TTL_HOURS * 3600,
    }).encode())
    sig = hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"

def verify_token(token: str) -> Optional[dict]:
    try:
        header, payload, sig = token.split(".")
        expected = _b64url_encode(
            hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_b64url_decode(payload))
        if claims["exp"] < time.time():
            return None
        return claims
    except Exception:
        return None

def get_claims(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    return verify_token(token) if token else None

# ── Login page ────────────────────────────────────────────────────────────────
def _login_page(error: str = "") -> str:
    err_html = f'<div class="error">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{AUTH_REALM} — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#08090d;display:flex;align-items:center;justify-content:center;
    min-height:100vh;font-family:'Space Mono',monospace;overflow:hidden}}
  body::before{{content:'';position:fixed;inset:0;
    background-image:linear-gradient(rgba(0,229,160,0.03) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,229,160,0.03) 1px,transparent 1px);
    background-size:40px 40px;pointer-events:none}}
  .card{{background:#0f1117;border:1px solid #1e2235;border-radius:16px;
    padding:40px 48px 48px;width:420px;
    box-shadow:0 0 80px rgba(0,229,160,0.06);position:relative;z-index:1;
    animation:fadeUp 0.4s ease both}}
  @keyframes fadeUp{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:translateY(0)}}}}
  .logo{{text-align:center;margin-bottom:28px}}
  .logo-icon{{font-size:2.4rem;margin-bottom:8px;display:block}}
  .logo-title{{font-family:'Syne',sans-serif;font-size:1.5rem;font-weight:800;
    background:linear-gradient(135deg,#fff 30%,#4d9eff 100%);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
  .logo-sub{{color:#5a6180;font-size:0.65rem;letter-spacing:0.1em;margin-top:4px}}
  label{{display:block;color:#5a6180;font-size:0.62rem;letter-spacing:0.1em;
    text-transform:uppercase;margin-bottom:6px;margin-top:20px}}
  input{{width:100%;background:#141720;border:1px solid #1e2235;border-radius:8px;
    padding:10px 14px;color:#e8eaf0;font-family:'Space Mono',monospace;font-size:0.85rem;
    outline:none;transition:border-color 0.2s}}
  input:focus{{border-color:#4d9eff;box-shadow:0 0 0 3px rgba(77,158,255,0.1)}}
  .btn{{display:block;width:100%;margin-top:28px;
    background:linear-gradient(135deg,#00e5a0,#4d9eff);border:none;border-radius:8px;
    padding:12px;color:#08090d;font-family:'Space Mono',monospace;
    font-size:0.78rem;font-weight:700;letter-spacing:0.06em;cursor:pointer;transition:opacity 0.2s}}
  .btn:hover{{opacity:0.9}}
  .error{{background:rgba(255,77,109,0.1);border:1px solid rgba(255,77,109,0.3);
    border-radius:6px;padding:10px 14px;color:#ff4d6d;font-size:0.72rem;margin-top:16px}}
  .hint{{color:#2a2e3d;font-size:0.6rem;text-align:center;margin-top:24px;line-height:1.6}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <span class="logo-icon">⚙️</span>
    <div class="logo-title">{AUTH_REALM}</div>
    <div class="logo-sub">// secure access required</div>
  </div>
  <form method="POST" action="/login">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    {err_html}
    <button class="btn" type="submit">AUTHENTICATE →</button>
  </form>
  <div class="hint">session expires after {SESSION_TTL_HOURS}h · all actions are logged</div>
</div>
</body>
</html>"""

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(docs_url=None, redoc_url=None)

_HOP_HEADERS = {
    "connection","keep-alive","proxy-authenticate","proxy-authorization",
    "te","trailers","transfer-encoding","upgrade","host",
}

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def get_login():
    return HTMLResponse(_login_page())

@app.post("/login")
async def post_login(request: Request,
                     username: str = Form(...), password: str = Form(...)):
    ip   = request.client.host if request.client else "unknown"

    if _is_locked_out(ip):
        log.warning(f"AUTH BLOCKED (rate limit) ip={ip}")
        return HTMLResponse(_login_page(
            f"Too many failed attempts. Try again in {LOCKOUT_SECONDS//60} minutes."),
            status_code=429)

    user = authenticate(username, password)
    if not user:
        _record_failure(ip)
        remaining = MAX_LOGIN_ATTEMPTS - _fail_tracker[ip]["count"]
        log.warning(f"AUTH FAIL  user={username!r}  ip={ip}  remaining={remaining}")
        hint = f" ({remaining} attempt(s) remaining)" if remaining > 0 else ""
        return HTMLResponse(_login_page(f"Invalid username or password.{hint}"), status_code=401)

    _clear_failures(ip)
    token    = create_token(user["username"], user.get("role", "viewer"))
    next_url = request.query_params.get("next", "/")
    log.info(f"AUTH OK    user={username!r}  role={user.get('role')}  ip={ip}")

    # Register session for tracking
    token_sig = token.split(".")[-1][:16]
    _register_session(token_sig, user["username"], ip)

    resp = RedirectResponse(url=next_url, status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE,
                    samesite="lax", max_age=SESSION_TTL_HOURS * 3600, path="/")
    return resp

@app.get("/logout")
async def logout(request: Request):
    claims = get_claims(request)
    if claims:
        log.info(f"LOGOUT     user={claims['sub']!r}")
        token = request.cookies.get(COOKIE_NAME, "")
        _revoke_session(token.split(".")[-1][:16] if token else "")
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "auth",
            "active_sessions": len(_get_active_sessions()),
            "time": datetime.now(timezone.utc).isoformat()}

@app.get("/auth/sessions")
async def sessions(request: Request):
    """Admin endpoint — list active sessions. Requires valid JWT."""
    claims = get_claims(request)
    if not claims or claims.get("role") != "admin":
        return Response(status_code=403)
    return {"sessions": _get_active_sessions()}

# ── WebSocket proxy — /_stcore/stream ─────────────────────────────────────────
# Streamlit uses a WebSocket at /_stcore/stream for all real-time UI updates.
# httpx cannot proxy WebSockets — we need a dedicated bidirectional tunnel.
# Auth: validate JWT cookie from the WS handshake headers.

@app.websocket("/_stcore/stream")
async def ws_proxy(websocket: WebSocket):
    # ── Validate JWT from cookie ──────────────────────────────────────────────
    cookie_header = websocket.headers.get("cookie", "")
    token = None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            token = part[len(COOKIE_NAME) + 1:]
            break

    claims = verify_token(token) if token else None
    if not claims:
        log.warning(f"WS AUTH FAIL  ip={websocket.client.host if websocket.client else '?'}")
        # Must accept before close to satisfy the ASGI state machine
        await websocket.accept()
        await websocket.close(code=1008)
        return

    # Accept with the subprotocol the browser requested (e.g. "streamlit")
    raw_proto = websocket.headers.get("sec-websocket-protocol", "")
    accept_proto = raw_proto.split(",")[0].strip() if raw_proto else None
    await websocket.accept(subprotocol=accept_proto)
    log.info(f"WS CONNECT user={claims['sub']!r} subprotocol={accept_proto!r}")

    # ── Build upstream URL ────────────────────────────────────────────────────
    qs = str(websocket.url).split("?", 1)[1] if "?" in str(websocket.url) else ""
    target_ws = f"{STREAMLIT_WS_URL}/_stcore/stream"
    if qs:
        target_ws += f"?{qs}"

    # Forward subprotocols — Streamlit requires "streamlit" subprotocol
    subprotocols = [s.strip() for s in raw_proto.split(",") if s.strip()] or None

    # ── Bridge using a done-event to coordinate clean shutdown ────────────────
    done = asyncio.Event()

    try:
        async with websockets.connect(
            target_ws,
            subprotocols=subprotocols,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as upstream:

            async def browser_to_upstream():
                try:
                    while not done.is_set():
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        data = msg.get("bytes") or (
                            msg.get("text", "").encode() if msg.get("text") else None
                        )
                        if data:
                            await upstream.send(data)
                except Exception:
                    pass
                finally:
                    done.set()

            async def upstream_to_browser():
                try:
                    async for message in upstream:
                        if done.is_set():
                            break
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass
                finally:
                    done.set()

            t1 = asyncio.create_task(browser_to_upstream())
            t2 = asyncio.create_task(upstream_to_browser())

            await done.wait()

            # Cancel whichever task is still running
            for t in (t1, t2):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    except Exception as exc:
        log.error(f"WS proxy error: {exc}")

    # ── Close browser WebSocket cleanly — only if still connected ─────────────
    try:
        await websocket.close()
    except Exception:
        pass  # Already closed by browser disconnect — that's fine

    log.info(f"WS CLOSE   user={claims['sub']!r}")

# ── HTTP reverse proxy — all other paths ─────────────────────────────────────
# Streamlit internal paths (/_stcore/health, /static, etc.) bypass JWT check.
# All other paths require a valid session cookie.

_BYPASS_AUTH = {"/_stcore/", "/static/", "/vendor/", "/favicon."}

@app.api_route("/{full_path:path}",
               methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
async def http_proxy(request: Request, full_path: str):
    path = "/" + full_path

    # Decide whether this path needs auth
    needs_auth = not any(path.startswith(p) for p in _BYPASS_AUTH)

    if needs_auth:
        claims = get_claims(request)
        if not claims:
            if request.method == "GET":
                return RedirectResponse(url=f"/login?next={path}", status_code=302)
            return Response(status_code=401)
        extra_headers = {
            "X-Authenticated-User": claims["sub"],
            "X-User-Role":          claims.get("role", "viewer"),
        }
    else:
        extra_headers = {}

    # Build target URL
    target = f"{STREAMLIT_URL}{path}"
    if request.url.query:
        target += f"?{request.url.query}"

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_HEADERS
    }
    fwd_headers.update(extra_headers)
    fwd_headers["X-Forwarded-For"]   = request.client.host if request.client else ""
    fwd_headers["X-Forwarded-Proto"] = request.url.scheme

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.request(
                method=request.method, url=target,
                headers=fwd_headers, content=body,
                follow_redirects=False,
            )
        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _HOP_HEADERS | {"content-encoding","content-length"}
        }
        return Response(content=resp.content, status_code=resp.status_code,
                        headers=resp_headers)

    except httpx.ConnectError:
        return HTMLResponse(
            "<h3 style='font-family:monospace;color:#f85149;padding:40px'>"
            "⚠️ SRE Agent is starting up — retry in a few seconds</h3>",
            status_code=503)
    except Exception as exc:
        log.error(f"HTTP proxy error: {exc}")
        return HTMLResponse(
            f"<h3 style='font-family:monospace;color:#f85149;padding:40px'>"
            f"Proxy error: {exc}</h3>",
            status_code=502)
