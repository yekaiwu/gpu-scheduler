import io
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from gsched import cli as gcli
from gsched.mcp_stdio import McpServer, SUPPORTED, TOOLS, serve
from gsched.spec import SCHEMA_ID, spec_json_schema

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def rpc(server, method, params=None, mid=2):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def init(version="2025-11-25", call=None):
    s = McpServer(call=call)
    r = s.handle({**INIT, "params": {**INIT["params"], "protocolVersion": version}})
    s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return s, r


def fake_call(ok=True, result=None, error=None, record=None):
    def call(method, params, door="cli", timeout=10.0, **kw):
        if record is not None:
            record.append((method, params, door, timeout))
        if ok:
            return {"ok": True, "result": result}
        return {"ok": False, "error": error}
    return call


@pytest.mark.parametrize("v", SUPPORTED)
def test_mcp_initialize_negotiates_supported_version(v):
    s, r = init(v)
    assert r["result"]["protocolVersion"] == v
    assert r["result"]["serverInfo"]["name"] == "gpu-scheduler" and r["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert "submit_job returns immediately" in r["result"]["instructions"]


def test_mcp_initialize_unknown_version_returns_latest():
    s, r = init("2099-01-01")
    assert r["result"]["protocolVersion"] == SUPPORTED[0]


def test_mcp_lifecycle_errors():
    s = McpServer()
    assert s.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})["error"]["code"] == -32002
    assert s.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"})["result"] == {}
    s, _ = init()
    assert rpc(s, "nope")["error"]["code"] == -32601
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None
    assert s.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": []})["error"]["code"] == -32602
    assert s.handle([1])["error"]["code"] == -32600


def test_mcp_tools_list_six_tools():
    s, _ = init()
    tools = rpc(s, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["submit_job", "get_job", "list_jobs", "cancel_job", "get_job_logs", "get_status"]
    assert all("inputSchema" in t and t["description"] for t in tools)
    assert all("outputSchema" in t for t in tools if t["name"] != "get_job_logs")
    s, _ = init("2025-03-26")
    old = rpc(s, "tools/list")["result"]["tools"]
    assert not any("outputSchema" in t for t in old)         # only for >= 2025-06-18


def test_mcp_schema_matches_validator():
    from pathlib import Path
    from gsched.config import Config
    from gsched.mcp_stdio import validate
    from gsched.spec import parse_spec
    schema = next(t for t in TOOLS if t["name"] == "submit_job")["inputSchema"]
    assert schema == spec_json_schema(schema_required=False)
    ex = Path(__file__).resolve().parents[2] / "examples" / "jobs"
    files = sorted(ex.glob("*.json"))
    assert files
    for f in files:
        doc = json.loads(f.read_text())
        assert validate(schema, {k: v for k, v in doc.items() if k != "schema"}) is None, f
        parse_spec(doc, Config.defaults(), door="cli")
    bad = {"project": "p", "memory_gb": 3}
    assert validate(schema, bad) is not None


def test_mcp_fills_schema_field():
    rec = []
    s, _ = init(call=fake_call(result={"job": "j", "state": "queued", "queue": "day", "warnings": []}, record=rec))
    args = {"project": "p", "command": ["true"], "cwd": "/tmp", "checkpoint": "atomic", "resources": {"memory_gib": 1}}
    r = rpc(s, "tools/call", {"name": "submit_job", "arguments": args})
    assert r["result"]["isError"] is False
    assert rec[0][1]["spec"]["schema"] == SCHEMA_ID and rec[0][2] == "mcp"
    assert json.loads(r["result"]["content"][0]["text"])["job"] == "j"
    assert r["result"]["structuredContent"]["state"] == "queued"
    s2, _ = init("2025-03-26", call=fake_call(result={"job": "j", "state": "queued"}))
    r2 = rpc(s2, "tools/call", {"name": "submit_job", "arguments": args})
    assert "structuredContent" not in r2["result"]


def test_mcp_invalid_arguments_and_unknown_tool():
    s, _ = init(call=fake_call(result={}))
    assert rpc(s, "tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602
    assert rpc(s, "tools/call", {"name": "get_job", "arguments": {}})["error"]["code"] == -32602
    assert rpc(s, "tools/call", {"name": "get_job", "arguments": {"job": "x", "wait_s": 31}})["error"]["code"] == -32602
    assert rpc(s, "tools/call", {"name": "get_job_logs", "arguments": {"job": "x", "tail_bytes": 99999}})["error"]["code"] == -32602
    assert rpc(s, "tools/call", {"name": "submit_job", "arguments": {"project": "p", "memory_gb": 1}})["error"]["code"] == -32602


def test_get_job_wait_s_capped_at_30():
    rec = []
    s, _ = init(call=fake_call(result={"job": "j", "state": "held"}, record=rec))
    rpc(s, "tools/call", {"name": "get_job", "arguments": {"job": "j", "wait_s": 30}})
    assert rec[0][1] == {"job": "j", "wait_s": 30} and rec[0][0] == "get"
    assert rpc(s, "tools/call", {"name": "get_job", "arguments": {"job": "j", "wait_s": 100}})["error"]["code"] == -32602


def test_mcp_daemon_error_is_tool_error():
    s, _ = init(call=fake_call(ok=False, error={"code": "forbidden", "message": "MCP submission is not enabled"}))
    r = rpc(s, "tools/call", {"name": "get_job", "arguments": {"job": "x"}})
    assert r["result"]["isError"] is True
    assert json.loads(r["result"]["content"][0]["text"])["error"]["code"] == "forbidden"


def test_mcp_daemon_down_is_tool_error():
    from gsched.client import ConnectError

    def down(*a, **k):
        raise ConnectError("/nope")
    s, _ = init(call=down)
    r = rpc(s, "tools/call", {"name": "get_status", "arguments": {}})
    assert r["result"]["isError"] is True and "gsched install" in r["result"]["content"][0]["text"]
    assert "error" not in r


def test_mcp_observe_mode_text_prefix():
    s, _ = init(call=fake_call(result={"state": "observe", "job": None, "would": {"would": "admit_now"}, "hint": "h"}))
    args = {"project": "p", "command": ["true"], "cwd": "/tmp", "checkpoint": "atomic", "resources": {"memory_gib": 1}}
    r = rpc(s, "tools/call", {"name": "submit_job", "arguments": args})
    assert r["result"]["content"][0]["text"].startswith("NOT QUEUED (observe mode): {")


def test_mcp_no_lease_in_any_output():
    leaky = {"job": "j", "state": "held", "lease": "SECRETLEASE", "nested": {"lease": "SECRETLEASE", "x": 1}}
    s, _ = init(call=fake_call(result=leaky))
    for name, args in (("get_job", {"job": "j"}), ("list_jobs", {}), ("cancel_job", {"job": "j"}), ("get_status", {})):
        r = rpc(s, "tools/call", {"name": name, "arguments": args})
        assert "SECRETLEASE" not in json.dumps(r)


def test_mcp_logs_tool_returns_text():
    s, _ = init(call=fake_call(result={"data": "line1\nline2\n", "offset": 12, "eof": True, "size": 12}))
    r = rpc(s, "tools/call", {"name": "get_job_logs", "arguments": {"job": "j"}})
    assert r["result"]["content"][0]["text"] == "line1\nline2\n" and "structuredContent" not in r["result"]


def test_mcp_tools_call_is_one_json_line():
    s = McpServer(call=fake_call(result={"jobs": []}))
    out = io.StringIO()
    serve([json.dumps(INIT), json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
           json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_jobs", "arguments": {}}}),
           "", "not json at all"], out, s)
    lines = out.getvalue().split("\n")
    assert lines[-1] == ""
    lines = lines[:-1]
    assert len(lines) == 3 and "Content-Length" not in out.getvalue()
    msgs = [json.loads(l) for l in lines]
    assert all(m["jsonrpc"] == "2.0" for m in msgs)
    assert msgs[2]["error"]["code"] == -32700 and msgs[2]["id"] is None
    assert msgs[1]["result"]["isError"] is False


def test_mcp_log_line_not_on_stdout(tmp_path):
    inp = "\n".join([json.dumps(INIT), json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]) + "\n"
    p = subprocess.run([sys.executable, "-m", "gsched", "mcp"], input=inp, capture_output=True, text=True,
                       env={**os.environ, "GSCHED_SOCKET": str(tmp_path / "none.sock")})
    assert p.returncode == 0
    for line in p.stdout.splitlines():
        assert json.loads(line)["jsonrpc"] == "2.0"                  # stdout is protocol only
    assert "before notifications/initialized" in p.stderr           # the warning went to stderr


def test_mcp_stray_print_cannot_corrupt_stdout():
    code = ("import sys, io; from gsched import mcp_stdio as m; sys.stdin = io.TextIOWrapper(io.BytesIO("
            "b'{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}\\n')); orig = m.serve\n"
            "def noisy(lines, out, server=None):\n    print('STRAY')\n    return orig(lines, out, server)\n"
            "m.serve = noisy\nm.main()")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "STRAY" not in p.stdout and json.loads(p.stdout)["result"] == {}
    assert "STRAY" in p.stderr


# ---- against a real daemon -------------------------------------------------------------------------------------------------------
def mcp_session(msgs, env_extra=None):
    inp = "\n".join(json.dumps(m) for m in msgs) + "\n"
    p = subprocess.run([sys.executable, "-m", "gsched", "mcp"], input=inp, capture_output=True, text=True, timeout=60)
    return [json.loads(l) for l in p.stdout.splitlines()]


def call_msg(mid, name, args):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": args}}


def rows(daemon_home):
    db = sqlite3.connect(os.path.join(daemon_home, "state", "gsched.sqlite"))
    db.row_factory = sqlite3.Row
    out = [dict(r) for r in db.execute("select * from job order by submitted_ms, id")]
    db.close()
    return out


def test_submit_job_returns_without_waiting(tdaemon):
    import time
    args = {"project": "p", "command": ["sleep", "100"], "cwd": "/tmp", "checkpoint": "atomic",
            "resources": {"memory_gib": 1, "gpu": "none"}, "runtime": {"estimate_s": 86400}}
    t0 = time.monotonic()
    out = mcp_session([INIT, call_msg(2, "submit_job", args)])
    assert time.monotonic() - t0 < 10
    res = out[1]["result"]
    assert res["isError"] is False and res["structuredContent"]["queue"] == "night"
    assert res["structuredContent"]["state"] == "queued"


def test_mcp_forbidden_without_project_opt_in(tdaemon):
    args = {"project": "q", "command": ["true"], "cwd": "/tmp", "checkpoint": "atomic", "resources": {"memory_gib": 1}}
    out = mcp_session([INIT, call_msg(2, "submit_job", args)])
    res = out[1]["result"]
    assert res["isError"] is True and json.loads(res["content"][0]["text"])["error"]["code"] == "forbidden"
    assert rows(str(tdaemon.home)) == []


def test_mcp_rejects_external_spawn(tdaemon):
    args = {"project": "p", "cwd": "/tmp", "checkpoint": "atomic", "spawn": "external", "resources": {"memory_gib": 1}}
    out = mcp_session([INIT, call_msg(2, "submit_job", args)])
    res = out[1]["result"]
    assert res["isError"] is True and json.loads(res["content"][0]["text"])["error"]["code"] == "forbidden"


def test_mcp_and_cli_submit_identical_rows(tdaemon, tmp_path, capsys):
    spec = {"schema": SCHEMA_ID, "project": "p", "name": "same", "command": ["sleep", "100"], "cwd": "/tmp",
            "checkpoint": "atomic", "resources": {"memory_gib": 2, "gpu": "none"}, "runtime": {"estimate_s": 600},
            "env": {"A": "b"}, "priority": 2}
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec))
    rc = gcli.main(["submit", "--file", str(f)])
    assert rc == 0
    capsys.readouterr()
    mcp_args = {k: v for k, v in spec.items() if k != "schema"}
    out = mcp_session([INIT, call_msg(2, "submit_job", mcp_args)])
    assert out[1]["result"]["isError"] is False
    r = rows(str(tdaemon.home))
    assert len(r) == 2
    skip = {"id", "enqueued_ms", "submitted_ms", "door", "client_pid", "checkpoint_dir", "held_ms", "started_ms",
            "pid", "pid_path", "pid_start_sec", "pid_start_usec", "pgid", "shim_pid", "shim_start_sec",
            "shim_start_usec", "state", "held_reason", "gate_released", "ready", "footprint_bytes",
            "peak_footprint_bytes", "eligible_since_ms"}
    a, b = ({k: v for k, v in row.items() if k not in skip} for row in r)
    assert a == b
    assert {r[0]["door"], r[1]["door"]} == {"cli", "mcp"}


def test_mcp_end_to_end_tools(tdaemon):
    args = {"project": "p", "command": ["sleep", "100"], "cwd": "/tmp", "checkpoint": "atomic",
            "resources": {"memory_gib": 1, "gpu": "none"}, "runtime": {"estimate_s": 60}}
    out = mcp_session([INIT, call_msg(2, "submit_job", args)])
    jid = out[1]["result"]["structuredContent"]["job"]
    out = mcp_session([INIT, call_msg(2, "get_job", {"job": jid}), call_msg(3, "list_jobs", {"project": "p"}),
                       call_msg(4, "get_status", {}), call_msg(5, "get_job_logs", {"job": jid}),
                       call_msg(6, "cancel_job", {"job": jid, "terminate": True})])
    assert out[1]["result"]["structuredContent"]["job"] == jid
    assert out[2]["result"]["structuredContent"]["jobs"][0]["job"] == jid
    assert "memory" in out[3]["result"]["structuredContent"]
    assert out[4]["result"]["isError"] is False
    assert json.loads(out[5]["result"]["content"][0]["text"])["error"]["code"] == "forbidden"     # mcp_terminate is off


def test_daemon_import_does_not_load_mcp():
    p = subprocess.run([sys.executable, "-I", "-c",
                        "import gsched.daemon, sys; assert 'gsched.mcp_stdio' not in sys.modules; print('ok')"],
                       capture_output=True, text=True, cwd="/tmp")
    assert p.stdout.strip() == "ok", p.stderr
