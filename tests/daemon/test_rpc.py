import json
import os
import signal
import socket
import sqlite3

import pytest

from gsched.config import Config
from gsched.daemon import Daemon, DaemonBusy
from gsched.errors import PlatformError
from tests.daemon.conftest import ClientError, Pending, client, project, run_ticks, spec


def start(d, clock, jid, n=2):
    run_ticks(d, clock, n)
    return d.platform.workload(jid)


def state(d, jid):
    return client(d, "get", job=jid)["state"]


def test_commit_before_response(daemon, clock):
    seen = []
    orig = daemon.send_and_close

    def wrapped(c, env):
        res = env.get("result") or {}
        jid = res.get("job")
        if isinstance(jid, str):
            db = sqlite3.connect(daemon.paths.db)
            row = db.execute("SELECT state FROM job WHERE id=?", (jid,)).fetchone()
            db.close()
            seen.append((jid, res.get("state"), row[0] if row else None))
        return orig(c, env)

    daemon.send_and_close = wrapped
    r = client(daemon, "submit", spec=spec())
    run_ticks(daemon, clock, 1)
    v = client(daemon, "get", job=r["job"])
    assert v["state"] == "held"
    assert seen[0][2] == "queued"                      # submit reply: row already committed
    assert seen[-1][1] == "held" and seen[-1][2] == "held"   # get reply: held visible to a second connection


def test_two_submits_one_exclusive_holder(daemon, clock):
    a = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    b = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 3)
    states = sorted([state(daemon, a), state(daemon, b)])
    assert states == ["held", "queued"]
    q = client(daemon, "get", job=b if state(daemon, b) == "queued" else a)
    assert "gpu" in q["blocked_by"] and "GPU is taken" in q["why"]
    assert daemon.store.check_invariants() == []


def test_observe_inserts_nothing(make_daemon, clock, plat):
    d = make_daemon(Config.defaults().replace(timezone="UTC"))
    project(d, "p")
    r = client(d, "submit", spec=spec())
    assert r["state"] == "observe" and r["job"] is None and "observe mode" in r["hint"]
    run_ticks(d, clock, 5)
    assert d.store.conn.execute("select count(*) from job").fetchone()[0] == 0
    assert plat.spawned == [] and plat.signals == []


def test_observe_explains_would(make_daemon, clock, plat):
    d = make_daemon(Config.defaults().replace(timezone="UTC"))
    project(d, "p")
    r = client(d, "submit", spec=spec())
    assert r["would"]["would"] == "admit_now"
    plat.set_mem(reclaimable_gib=1)
    run_ticks(d, clock, 1)
    r = client(d, "submit", spec=spec(resources={"memory_gib": 10}))
    assert r["would"]["would"] == "queue" and "memory_live" in r["would"]["blocked_by"] and r["would"]["why"]
    r = client(d, "submit", spec=spec(runtime={"estimate_s": None}))
    assert r["would"]["would"] == "needs_estimate"
    d.store.meta_get("x")


def test_observe_spawns_nothing_even_with_rows(make_daemon, clock, plat):
    d = make_daemon(Config.defaults().replace(timezone="UTC"))
    project(d, "p")
    run_ticks(d, clock, 3)
    assert plat.spawned == []


def test_socket_close_after_submit_does_not_cancel(daemon, clock):
    p = Pending(daemon, "submit", spec=spec())
    r = None
    while r is None:
        r = p.poll()
    p.close()
    run_ticks(daemon, clock, 3)
    assert state(daemon, r["result"]["job"]) == "held"


def test_unknown_project_no_row_exit_2(daemon):
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(project="nope"))
    assert e.value.code == "unknown_project"
    assert daemon.store.conn.execute("select count(*) from job").fetchone()[0] == 0
    from gsched.errors import GschedError
    assert GschedError("unknown_project", "x").exit_code == 2


def test_submit_validation_order_and_errors(daemon):
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(resources={"memory_gib": 9999}))
    assert e.value.code == "invalid"           # beyond the spec maximum
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(resources={"memory_gib": 100}))
    assert e.value.code == "too_big"
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(resources={"cpu_cores": 100}))
    assert e.value.code == "too_big"
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(command=["definitely-not-a-command-xyz"]))
    assert e.value.code == "command_not_found"
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(cwd="/no/such/dir"))
    assert e.value.code == "invalid"
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(after=["20260101T000000-abcd"]))
    assert e.value.code == "invalid" and e.value.err["detail"]["unknown"]
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec={"schema": "x"})
    assert e.value.code == "schema"


def test_idempotency_key_returns_same_job(daemon):
    a = client(daemon, "submit", spec=spec(idempotency_key="k1"))
    b = client(daemon, "submit", spec=spec(idempotency_key="k1"))
    assert b["job"] == a["job"] and b["idempotent"] is True


def test_needs_estimate_flow(daemon, clock):
    r = client(daemon, "submit", spec=spec(runtime={"estimate_s": None}))
    assert r["state"] == "needs_estimate"
    run_ticks(daemon, clock, 2)
    v = client(daemon, "get", job=r["job"])
    assert v["state"] == "needs_estimate" and "gsched estimate" in v["why"]
    v = client(daemon, "estimate", job=r["job"], estimate_s=60)
    assert v["state"] == "queued"
    run_ticks(daemon, clock, 2)
    assert state(daemon, r["job"]) == "held"


def test_mcp_requires_opt_in(daemon):
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", door="mcp", spec=spec())
    assert e.value.code == "forbidden"
    project(daemon, "p", mcp_submit=True)
    r = client(daemon, "submit", door="mcp", spec=spec())
    assert r["state"] == "queued"
    ext = spec(spawn="external")
    del ext["command"]
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", door="mcp", spec=ext)
    assert e.value.code == "forbidden"


def test_mcp_needs_cwd_or_default(daemon):
    project(daemon, "p", mcp_submit=True)
    s = spec()
    del s["cwd"]
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", door="mcp", spec=s)
    assert e.value.code == "invalid" and e.value.err["detail"]["field"] == "cwd"
    project(daemon, "p", mcp_submit=True, default_cwd="/tmp")
    assert client(daemon, "submit", door="mcp", spec=s)["state"] == "queued"


def test_command_allow(daemon):
    project(daemon, "p", command_allow=["/usr/bin/"])
    assert client(daemon, "submit", spec=spec(command=["true"]))["state"] == "queued"
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec(command=["/bin/sleep", "1"]))
    assert e.value.code == "forbidden"


def test_drain_refuses_submit(daemon):
    client(daemon, "drain")
    with pytest.raises(ClientError) as e:
        client(daemon, "submit", spec=spec())
    assert e.value.code == "draining"
    client(daemon, "undrain")
    assert client(daemon, "submit", spec=spec())["state"] == "queued"


def test_request_too_large(daemon):
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(daemon.socket_path))
    s.setblocking(False)
    data = memoryview(b"x" * (1024 * 1024 + 10))
    buf = b""
    for _ in range(2000):
        try:
            n = s.send(data[:65536])
            data = data[n:]
        except (BlockingIOError, BrokenPipeError):
            pass
        daemon.step(0)
        try:
            buf += s.recv(65536)
        except BlockingIOError:
            pass
        if b"\n" in buf:
            break
    assert json.loads(buf.split(b"\n")[0])["error"]["code"] == "invalid"
    s.close()


def test_bad_json_and_nan(daemon):
    r = client(daemon, "nope", raw=True)
    assert r["error"]["code"] == "invalid"
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(daemon.socket_path))
    s.sendall(b'{"rpc":"gpu-scheduler.rpc.v1","method":"ping","params":{"x":NaN}}\n')
    s.setblocking(False)
    out = b""
    for _ in range(50):
        daemon.step(0)
        try:
            out += s.recv(65536)
        except BlockingIOError:
            pass
        if out:
            break
    assert json.loads(out)["error"]["code"] == "schema"


def test_peer_uid_mismatch_forbidden(daemon, plat):
    plat.peer_uid = os.getuid() + 1
    r = client(daemon, "ping", raw=True)
    assert r["error"]["code"] == "forbidden"


def test_idle_connection_closed(daemon, clock):
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(daemon.socket_path))
    s.setblocking(False)
    daemon.step(0)
    clock.advance(11)
    daemon.step(0)
    daemon.step(0)
    assert s.recv(10) == b""
    s.close()


def test_socket_path_too_long_exits_78(short_tmp, clock, plat):
    cfg = Config.defaults().replace(socket_path="/tmp/" + "x" * 120 + ".sock")
    d = Daemon(short_tmp, cfg=cfg, clock=clock, platform=plat, log_enabled=False)
    with pytest.raises(PlatformError) as e:
        d.start()
    assert "too long" in str(e.value)


def test_single_instance_lock(short_tmp, clock, plat, daemon):
    d2 = Daemon(short_tmp, cfg=Config.defaults(), clock=clock, platform=plat, log_enabled=False)
    with pytest.raises(DaemonBusy):
        d2.start()
    assert client(daemon, "ping")["pid"] == os.getpid()      # the first daemon is untouched


def test_shutdown_does_not_signal_jobs(make_daemon, clock, plat):
    d = make_daemon()
    project(d, "p")
    jid = client(d, "submit", spec=spec())["job"]
    start(d, clock, jid)
    d.stop()
    assert plat.signals == []
    assert not os.path.exists(d.paths.pidfile)


def test_status_brief_and_full(daemon, clock):
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 8, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 3)
    st = client(daemon, "status")
    assert st["enforce"] and st["memory"]["budget_gib"] == 40 and st["running"][0]["job"] == jid
    assert st["gpu"]["exclusive_job"] == jid and st["memory"]["reserved_gib"] == 8
    b = client(daemon, "status", brief=True)
    assert b["queue"] == [] and "memory" in b


def test_list_and_since(daemon, clock):
    a = client(daemon, "submit", spec=spec())["job"]
    start(daemon, clock, a).exit(0)
    run_ticks(daemon, clock, 2)
    assert state(daemon, a) == "done"
    assert [j["job"] for j in client(daemon, "list")["jobs"]] == [a]          # the RPC lists every state by default
    assert client(daemon, "list", state=["queued", "held"])["jobs"] == []
    done = client(daemon, "list", state=["done"], since_ms=0)["jobs"]
    assert [j["job"] for j in done] == [a] and "command" not in done[0]
    assert client(daemon, "list", state=["done"], since_ms=daemon.clock.now_ms() + 10**6)["jobs"] == []


def test_logs_rpc(daemon, clock):
    jid = client(daemon, "submit", spec=spec())["job"]
    w = start(daemon, clock, jid)
    w.write_stdout("hello world\n")
    r = client(daemon, "logs", job=jid)
    assert r["data"] == "hello world\n" and r["eof"]
    w.write_stdout("more\n")
    r2 = client(daemon, "logs", job=jid, offset=r["offset"])
    assert r2["data"] == "more\n"
    assert client(daemon, "logs", job=jid, stream="stderr")["data"] == ""
    r3 = client(daemon, "logs", job=jid, tail_bytes=3)
    assert r3["data"] == "re\n"
    with pytest.raises(ClientError):
        client(daemon, "logs", job=jid, door="mcp", tail_bytes=99999)


def test_events_rpc_filters_and_limits(daemon, clock):
    for i in range(5):
        daemon.store.add_event(clock.now_ms() - 1000 * (5 - i), "sample" if i % 2 else "admit", None, {"i": i})
    evs = client(daemon, "events", since_ms=0)["events"]
    assert [e["body"]["i"] for e in evs if "i" in e["body"]] == [0, 1, 2, 3, 4]
    assert all(e["kind"] == "sample" for e in client(daemon, "events", since_ms=0, kinds=["sample"])["events"])
    assert len(client(daemon, "events", since_ms=0, limit=2)["events"]) == 2
    ex = client(daemon, "events", since_ms=0, exclude_kinds=["sample"])["events"]
    assert all(e["kind"] != "sample" for e in ex)


def test_daemon_forbids_mutation_from_dashboard_door(daemon):
    for m, params in (("submit", {"spec": spec()}), ("cancel", {"job": "x"}), ("drain", {}), ("reload", {}),
                      ("attach", {"lease": "x", "pid": 5}), ("ensure", {"profile": "x"}), ("estimate", {})):
        r = client(daemon, m, door="dashboard", raw=True, **params)
        assert r["error"]["code"] == "forbidden", m
    for m in ("status", "ping", "list"):
        assert client(daemon, m, door="dashboard", raw=True)["ok"]
