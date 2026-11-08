"""`gsched dashboard`: a read-only, loopback-only, token-protected web view (docs/implementation/15-dashboard.md).

A separate process from the daemon (the daemon opens no TCP port). It talks to the daemon through the same Unix socket
as everything else, with door="dashboard", and may call only read-only methods. The one-thread rule applies to the
daemon only; this server uses ThreadingHTTPServer.
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.parse

from gsched import client
from gsched.paths import Paths, mkdir_private

ALLOWED_METHODS = frozenset({"status", "list", "get", "logs", "events", "ping"})
ALLOWED_HOSTS = ("127.0.0.1", "::1", "localhost")
JOB_ID_RE = re.compile(r"^[0-9T]{15}-[0-9a-f]{4}$")
TERMINAL = ["done", "failed", "cancelled", "lost"]
LIVE = ["needs_estimate", "queued", "held"]
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'")
COOKIE = "gsched_t"
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_page.html")


class DaemonUnreachable(Exception):
    pass


def daemon_call(method: str, params: dict | None = None):
    """The ONLY way this process talks to the daemon. Enforces the read-only allowlist."""
    if method not in ALLOWED_METHODS:
        raise PermissionError(f"dashboard may not call {method!r}")
    try:
        env = client.call(method, params or {}, door="dashboard", timeout=10.0)
    except (client.ConnectError, client.ProtocolError, OSError) as e:
        raise DaemonUnreachable(str(e))
    if not env.get("ok"):
        err = env.get("error", {})
        raise DaemonError(err.get("code", "internal"), err.get("message", ""))
    return env["result"]


class DaemonError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def token_path(paths: Paths | None = None) -> str:
    return (paths or Paths.resolve()).p("run", "dashboard.token")


def load_or_create_token(paths: Paths | None = None) -> str:
    paths = paths or Paths.resolve()
    path = token_path(paths)
    try:
        tok = open(path).read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    mkdir_private(paths.run)
    tok = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    os.chmod(path, 0o600)
    return tok


def sanitize_job(v: dict) -> dict:
    """The page is a browser surface: no env keys, and the command is only argv0's basename plus an arg count."""
    v = dict(v)
    v.pop("env_keys", None)
    v.pop("checkpoint_dir", None)
    cmd = v.pop("command", None)
    if cmd:
        v["command_summary"] = f"{os.path.basename(cmd[0])} (+{len(cmd) - 1} args)"
    return v


def timeline_points(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        b = e.get("body", {})
        out.append({"t_ms": e["t_ms"], "reclaimable_gib": b.get("reclaimable_gib"), "reserved_gib": b.get("reserved_gib"),
                    "pending_gib": b.get("pending_gib"), "budget_gib": b.get("budget_gib"),
                    "swap_used_gib": b.get("swap_used_gib"), "level": b.get("level"), "gpu_util": b.get("gpu_util"),
                    "running": b.get("running")})
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "gsched-dashboard"
    protocol_version = "HTTP/1.0"

    # quiet logging (never log tokens)
    def log_message(self, fmt, *args):
        pass

    # ---- plumbing -----------------------------------------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json; charset=utf-8", extra)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _cookie_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    # ---- requests -----------------------------------------------------------------------------------------------
    def do_GET(self):
        self._handle()

    do_HEAD = do_GET

    def do_POST(self):
        self._json(405, {"error": "read_only"}, {"Allow": "GET"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _handle(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad_host"})
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        given_q = (qs.get("t") or [None])[0]
        given_c = self._cookie_token()
        tok = self.server.token
        # compare bytes: compare_digest() raises TypeError on non-ASCII str (a 500 and a traceback per probe)
        good_q = given_q is not None and hmac.compare_digest(given_q.encode("utf-8", "replace"), tok.encode())
        good_c = given_c is not None and hmac.compare_digest(given_c.encode("utf-8", "replace"), tok.encode())
        if not (good_q or good_c):
            return self._json(403, {"error": "forbidden"})
        extra = {}
        if good_q and not good_c:
            extra["Set-Cookie"] = f"{COOKIE}={tok}; HttpOnly; SameSite=Strict; Path=/"
        try:
            self._route(url.path, qs, extra)
        except DaemonUnreachable:
            self._json(503, {"error": "daemon_unreachable"}, extra)
        except DaemonError as e:
            self._json(404 if e.code == "unknown_job" else 502, {"error": e.code, "message": str(e)}, extra)
        except PermissionError as e:           # the allowlist: a bug if it ever fires
            self._json(500, {"error": "internal", "message": str(e)}, extra)

    def _route(self, path: str, qs: dict, extra: dict) -> None:
        now = int(time.time() * 1000)
        if path == "/":
            with open(PAGE_PATH, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8", extra)
        if path == "/api/status":
            return self._json(200, daemon_call("status"), extra)
        if path == "/api/jobs":
            active = daemon_call("list", {"state": LIVE, "limit": 500})["jobs"]
            recent = daemon_call("list", {"state": TERMINAL, "since_ms": now - 24 * 3600 * 1000, "limit": 200})["jobs"]
            return self._json(200, {"active": active, "recent": recent}, extra)
        m = re.fullmatch(r"/api/job/([^/]+)", path)
        if m:
            if not JOB_ID_RE.match(m.group(1)):
                return self._json(400, {"error": "bad_job_id"}, extra)
            return self._json(200, sanitize_job(daemon_call("get", {"job": m.group(1)})), extra)
        m = re.fullmatch(r"/api/logs/([^/]+)", path)
        if m:
            if not JOB_ID_RE.match(m.group(1)):
                return self._json(400, {"error": "bad_job_id"}, extra)
            stream = (qs.get("stream") or ["stdout"])[0]
            if stream not in ("stdout", "stderr"):
                return self._json(400, {"error": "bad_stream"}, extra)
            r = daemon_call("logs", {"job": m.group(1), "stream": stream, "tail_bytes": 16384})
            return self._send(200, r["data"].encode("utf-8", "replace"), "text/plain; charset=utf-8", extra)
        if path == "/api/timeline":
            hours = _hours(qs, 6)
            ev = daemon_call("events", {"kinds": ["sample"], "since_ms": now - int(hours * 3600 * 1000), "limit": 10000})
            return self._json(200, timeline_points(ev["events"]), extra)
        if path == "/api/events":
            hours = _hours(qs, 24)
            ev = daemon_call("events", {"exclude_kinds": ["sample"], "since_ms": now - int(hours * 3600 * 1000),
                                        "limit": 200, "newest_first": True})
            return self._json(200, ev["events"], extra)
        self._json(404, {"error": "not_found"}, extra)


def _hours(qs: dict, default: float) -> float:
    try:
        return max(0.1, min(168.0, float((qs.get("hours") or [default])[0])))
    except ValueError:
        return default


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, token: str):
        self.address_family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        self.token = token
        super().__init__((host, port), Handler)


def normalize_host(host: str) -> str | None:
    if host not in ALLOWED_HOSTS:
        return None
    return "127.0.0.1" if host == "localhost" else host


def run_dashboard(args) -> int:
    host = normalize_host(args.host)
    if host is None:
        print(f"gsched dashboard: --host must be one of {', '.join(ALLOWED_HOSTS)} (loopback only)", file=sys.stderr)
        return 64
    token = load_or_create_token()
    shown = "[::1]" if host == "::1" else host
    url = f"http://{shown}:{args.port}/?t={token}"
    if args.print_url:
        print(url)
        return 0
    try:
        srv = DashboardServer(host, args.port, token)
    except OSError as e:
        print(f"gsched dashboard: cannot listen on {host}:{args.port}: {e}", file=sys.stderr)
        return 78
    print(f"gsched dashboard: serving on http://{shown}:{srv.server_address[1]}/ (read-only, token required)",
          file=sys.stderr)
    if sys.stdout.isatty():              # never write the token into a launchd log file
        print(url)
        sys.stdout.flush()
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
