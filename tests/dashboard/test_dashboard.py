import http.client
import json
import os
import re
import stat
import threading
from pathlib import Path

import pytest

from gsched import cli as gcli
from gsched import client as gclient
from gsched import dashboard as dash
from tests.mcp.conftest import tdaemon  # noqa: F401  (fixture)

JOB = "20261108T231500-7f3a"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("GSCHED_HOME", str(tmp_path / "h"))
    srv = dash.DashboardServer("127.0.0.1", 0, "tok-ABC")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.port = srv.server_address[1]
    yield srv
    srv.shutdown()
    srv.server_close()


def get(srv, path, headers=None, method="GET", host=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = dict(headers or {})
    if host:
        h["Host"] = host
    c.request(method, path, headers=h)
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict(r.getheaders()), body)
    c.close()
    return out


def fake_daemon(monkeypatch, record=None, down=False):
    status = {"enforce": True, "now_ms": 1, "machine": {"chip": "x", "memsize_gib": 48, "os": "27"}, "memory": {},
              "running": [], "queue": [], "gpu": {}, "cpu": {}}
    job = {"job": JOB, "state": "held", "command": ["/usr/bin/python3", "-c", "secret-arg", "x"], "env_keys": ["TOKEN"],
           "cwd": "/tmp", "checkpoint_dir": "/tmp/ck", "resources": {"memory_gib": 1}}

    def call(method, params=None, door="cli", timeout=10.0, **kw):
        if record is not None:
            record.append((method, dict(params or {}), door))
        if down:
            raise gclient.ConnectError("/x")
        if method == "status":
            return {"ok": True, "result": status}
        if method == "list":
            return {"ok": True, "result": {"jobs": [{"job": JOB, "state": "queued"}]}}
        if method == "get":
            return {"ok": True, "result": dict(job)}
        if method == "logs":
            return {"ok": True, "result": {"data": "line\n", "offset": 5, "eof": True, "size": 5}}
        if method == "events":
            if params.get("kinds") == ["sample"]:
                return {"ok": True, "result": {"events": [{"t_ms": 10, "kind": "sample", "job": None,
                        "body": {"reclaimable_gib": 20.5, "reserved_gib": 4, "pending_gib": 1, "budget_gib": 40,
                                 "swap_used_gib": 0.1, "level": 80, "gpu_util": 3, "running": 2, "free_gib": 9}}]}}
            return {"ok": True, "result": {"events": [{"t_ms": 11, "kind": "admit", "job": JOB, "body": {}}]}}
        return {"ok": True, "result": {}}
    monkeypatch.setattr(gclient, "call", call)


def test_dashboard_binds_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"
    assert dash.normalize_host("localhost") == "127.0.0.1" and dash.normalize_host("::1") == "::1"
    for bad in ("0.0.0.0", "192.168.1.5", "::", "example.com", ""):
        assert dash.normalize_host(bad) is None


def test_dashboard_rejects_non_loopback_host_arg(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GSCHED_HOME", str(tmp_path / "h"))
    assert gcli.main(["dashboard", "--host", "0.0.0.0"]) == 64
    assert "loopback" in capsys.readouterr().err
    assert not (tmp_path / "h" / "run" / "dashboard.token").exists()          # nothing was created


def test_dashboard_requires_token(server, monkeypatch):
    fake_daemon(monkeypatch)
    for path in ("/", "/api/status", "/api/jobs", f"/api/job/{JOB}", "/api/timeline", "/api/events", f"/api/logs/{JOB}"):
        assert get(server, path)[0] == 403, path
        assert get(server, path + ("&" if "?" in path else "?") + "t=wrong")[0] == 403
    assert get(server, "/api/status", {"Cookie": "gsched_t=nope"})[0] == 403
    st, hdr, body = get(server, "/api/status?t=tok-ABC")
    assert st == 200
    sc = hdr["Set-Cookie"]
    assert "gsched_t=tok-ABC" in sc and "HttpOnly" in sc and "SameSite=Strict" in sc
    assert get(server, "/api/status", {"Cookie": "other=1; gsched_t=tok-ABC"})[0] == 200
    assert "Set-Cookie" not in get(server, "/api/status", {"Cookie": "gsched_t=tok-ABC"})[1]


def test_dashboard_bad_host_header_421(server, monkeypatch):
    fake_daemon(monkeypatch)
    assert get(server, "/api/status?t=tok-ABC", host="evil.example")[0] == 421
    assert get(server, "/api/status?t=tok-ABC", host="127.0.0.1")[0] == 421                 # missing port
    assert get(server, "/api/status?t=tok-ABC", host=f"127.0.0.1:{server.port + 1}")[0] == 421
    assert get(server, "/api/status?t=tok-ABC", host=f"localhost:{server.port}")[0] == 200
    assert get(server, "/api/status?t=tok-ABC", host=f"[::1]:{server.port}")[0] == 200
    assert get(server, "/api/status", host="evil.example")[0] == 421                        # host check precedes the token


def test_dashboard_security_headers(server, monkeypatch):
    fake_daemon(monkeypatch)
    st, h, body = get(server, "/?t=tok-ABC")
    assert st == 200 and b"<title>gsched dashboard</title>" in body
    assert h["Content-Security-Policy"] == ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' "
                                            "'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
    assert h["X-Frame-Options"] == "DENY" and h["Cache-Control"] == "no-store" and h["Referrer-Policy"] == "no-referrer"


def test_dashboard_only_readonly_methods(server, monkeypatch):
    rec = []
    fake_daemon(monkeypatch, record=rec)
    for path in ("/", "/api/status", "/api/jobs", f"/api/job/{JOB}", f"/api/logs/{JOB}?stream=stderr",
                 "/api/timeline?hours=6", "/api/events?hours=24"):
        assert get(server, path + ("&" if "?" in path else "?") + "t=tok-ABC")[0] == 200, path
    assert {m for m, _, _ in rec} <= dash.ALLOWED_METHODS and {m for m, _, _ in rec} >= {"status", "list", "get", "logs", "events"}
    assert {d for _, _, d in rec} == {"dashboard"}
    for method in ("submit", "cancel", "drain", "undrain", "ensure", "attach", "finish", "release", "estimate", "reload", "gc"):
        with pytest.raises(PermissionError):
            dash.daemon_call(method, {})
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        c = http.client.HTTPConnection("127.0.0.1", server.port)
        c.request(m, "/api/status?t=tok-ABC", body=b"{}")
        assert c.getresponse().status == 405
        c.close()
    assert not [m for m, _, _ in rec if m not in dash.ALLOWED_METHODS]


def test_dashboard_html_has_no_external_urls():
    html = Path(dash.PAGE_PATH).read_text()
    stripped = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    assert "http://" not in stripped and "https://" not in stripped
    assert "<script src" not in html and "<link" not in html and "@import" not in html and "url(http" not in html
    assert "innerHTML" not in html                                         # data is only ever inserted as text


def test_dashboard_job_view_strips_env_and_args(server, monkeypatch):
    fake_daemon(monkeypatch)
    st, _, body = get(server, f"/api/job/{JOB}?t=tok-ABC")
    j = json.loads(body)
    assert st == 200 and "env_keys" not in j and "command" not in j and "checkpoint_dir" not in j
    assert j["command_summary"] == "python3 (+3 args)"
    assert "secret-arg" not in body.decode() and "TOKEN" not in body.decode()
    assert get(server, "/api/job/../../etc?t=tok-ABC")[0] in (400, 404)
    assert get(server, "/api/job/not-an-id?t=tok-ABC")[0] == 400
    assert get(server, f"/api/logs/{JOB}?stream=bogus&t=tok-ABC")[0] == 400
    st, hdr, body = get(server, f"/api/logs/{JOB}?stream=stdout&t=tok-ABC")
    assert st == 200 and body == b"line\n" and hdr["Content-Type"].startswith("text/plain")


def test_dashboard_503_when_daemon_down(server, monkeypatch):
    fake_daemon(monkeypatch, down=True)
    for path in ("/api/status", "/api/jobs", f"/api/job/{JOB}", "/api/timeline", "/api/events"):
        st, _, body = get(server, path + "?t=tok-ABC")
        assert st == 503 and json.loads(body) == {"error": "daemon_unreachable"}, path
    assert get(server, "/?t=tok-ABC")[0] == 200            # the page itself still serves (it shows a banner)


def test_timeline_shape(server, monkeypatch):
    fake_daemon(monkeypatch)
    st, _, body = get(server, "/api/timeline?hours=6&t=tok-ABC")
    pts = json.loads(body)
    assert st == 200 and pts == [{"t_ms": 10, "reclaimable_gib": 20.5, "reserved_gib": 4, "pending_gib": 1,
                                  "budget_gib": 40, "swap_used_gib": 0.1, "level": 80, "gpu_util": 3, "running": 2}]


def test_jobs_endpoint_shape(server, monkeypatch):
    rec = []
    fake_daemon(monkeypatch, record=rec)
    st, _, body = get(server, "/api/jobs?t=tok-ABC")
    j = json.loads(body)
    assert set(j) == {"active", "recent"}
    lists = [p for m, p, _ in rec if m == "list"]
    assert lists[0]["state"] == ["needs_estimate", "queued", "held"]
    assert lists[1]["state"] == ["done", "failed", "cancelled", "lost"] and lists[1]["since_ms"] > 0


def test_token_file_created_once_mode_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("GSCHED_HOME", str(tmp_path / "h"))
    t1 = dash.load_or_create_token()
    p = tmp_path / "h" / "run" / "dashboard.token"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and len(t1) >= 30
    assert dash.load_or_create_token() == t1


def test_print_url(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GSCHED_HOME", str(tmp_path / "h"))
    assert gcli.main(["dashboard", "--print-url", "--port", "9999"]) == 0
    url = capsys.readouterr().out.strip()
    tok = dash.load_or_create_token()
    assert url == f"http://127.0.0.1:9999/?t={tok}"


# ---- against a real daemon ---------------------------------------------------------------------------------------
def test_dashboard_against_real_daemon(tdaemon, monkeypatch, tmp_path):
    monkeypatch.setenv("GSCHED_HOME", str(tmp_path / "dh"))
    srv = dash.DashboardServer("127.0.0.1", 0, "tok-REAL")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.port = srv.server_address[1]
    try:
        spec = {"schema": "gpu-scheduler.job.v1", "project": "p", "command": ["sleep", "100"], "cwd": "/tmp",
                "checkpoint": "atomic", "resources": {"memory_gib": 1, "gpu": "none"}, "runtime": {"estimate_s": 60},
                "env": {"SECRET_ENV": "hunter2"}}
        r = gclient.call("submit", {"spec": spec})
        jid = r["result"]["job"]
        import time
        for _ in range(100):
            st, _, body = get(srv, "/api/status?t=tok-REAL")
            if json.loads(body)["running"]:
                break
            time.sleep(0.05)
        s = json.loads(body)
        assert st == 200 and s["running"][0]["job"] == jid and "memory" in s
        jobs = json.loads(get(srv, "/api/jobs?t=tok-REAL")[2])
        assert [j["job"] for j in jobs["active"]] == [jid]
        job = json.loads(get(srv, f"/api/job/{jid}?t=tok-REAL")[2])
        assert job["command_summary"] == "sleep (+1 args)" and "hunter2" not in json.dumps(job) and "env_keys" not in job
        assert get(srv, f"/api/logs/{jid}?t=tok-REAL")[0] == 200
        assert json.loads(get(srv, "/api/events?hours=24&t=tok-REAL")[2])
        # the daemon itself refuses mutations arriving through the dashboard door
        env = gclient.call("cancel", {"job": jid}, door="dashboard")
        assert env["ok"] is False and env["error"]["code"] == "forbidden"
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_non_ascii_token_is_403_not_500(server, monkeypatch):
    fake_daemon(monkeypatch)
    assert get(server, "/api/status?t=%C3%A9t%C3%A9")[0] == 403
    assert get(server, "/api/status", {"Cookie": "gsched_t=%C3%A9"})[0] == 403


def test_dashboard_page_shows_reservations_in_header_and_timeline():
    from gsched import dashboard
    html = open(dashboard.PAGE_PATH).read()
    assert "s.reservations" in html and '"window " + r.name' in html           # header strip badges
    assert "r.windows" in html and "time reservation (shaded)" in html          # timeline shading + legend
    assert "reservation_window:" in html                                         # blocked_by chip class
