import json
import os

import pytest

from gsched.log import DecisionLog
from gsched.units import GiB
from tests.daemon.conftest import client, project, run_ticks, spec


def add_hist(d, project, name, peak_gib, runtime_s, n=1, outcome="done"):
    for i in range(n):
        d.store.conn.execute(
            "INSERT INTO history(job,attempt,project,name,checkpoint,gpu,memory_bytes,peak_footprint_bytes,runtime_s,"
            "outcome,t_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"j{i}", 1, project, name, "atomic", "none", 8 * GiB, int(peak_gib * GiB), runtime_s, outcome, 1000 + i))


def test_suggest_p95(daemon):
    assert client(daemon, "suggest", project="p", name="sweep") == {"n": 0, "note": "not enough history"}
    add_hist(daemon, "p", "sweep", 10.0, 600, n=9)
    add_hist(daemon, "p", "sweep", 14.0, 3600, n=1)
    add_hist(daemon, "p", "sweep", 99.0, 5, n=3, outcome="failed")        # failures are ignored
    s = client(daemon, "suggest", project="p", name="sweep")
    assert s["n"] == 10 and s["peak_gib_max"] == 14.0
    assert s["memory_gib_p95"] == 15.5                                    # ceil_to_0.5(14 * 1.10)
    assert s["runtime_s_p50"] == 600
    s2 = client(daemon, "suggest", project="p")
    assert s2["n"] == 10


def test_submit_warns_low_memory_vs_history(daemon):
    add_hist(daemon, "p", "sweep", 14.0, 3600, n=5)
    r = client(daemon, "submit", spec=spec(name="sweep", resources={"memory_gib": 8, "gpu": "none"},
                                           runtime={"estimate_s": 100}))
    assert any("below the p95 peak" in w for w in r["warnings"])
    assert any("estimate looks short" in w for w in r["warnings"])
    r = client(daemon, "submit", spec=spec(name="sweep", resources={"memory_gib": 20, "gpu": "none"},
                                           runtime={"estimate_s": 3600}))
    assert r["warnings"] == []


def test_submit_warns_wired_limit(daemon):
    r = client(daemon, "submit", spec=spec(resources={"memory_gib": 36, "gpu": "exclusive"}))
    assert any("wired-memory" in w for w in r["warnings"])


def test_every_decision_writes_event(daemon, clock, plat):
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 3)
    plat.workload(jid).exit(0)
    run_ticks(daemon, clock, 2)
    kinds = {e["kind"] for e in daemon.store.events(job=jid, since_ms=0)}
    assert {"submit", "admit", "spawn", "gate_released", "exit"} <= kinds
    admit = [e for e in daemon.store.events(job=jid, since_ms=0) if e["kind"] == "admit"][0]["body"]
    assert admit["reserved_after_bytes"] > admit["reserved_before_bytes"] and "reclaimable_bytes" in admit


def test_no_env_values_in_log_or_events(make_daemon, clock, plat):
    d = make_daemon(log_enabled=True)
    project(d, "p")
    jid = client(d, "submit", spec=spec(env={"API_TOKEN": "hunter2-secret"}, command=["sleep", "5", "arg-secret-zzz"],
                                       comment="note"))["job"]
    run_ticks(d, clock, 3)
    d.residents  # noqa
    client(d, "get", job=jid)
    client(d, "list")
    client(d, "status")
    d.log.close()
    logtxt = open(d.paths.log_file).read()
    events = json.dumps(d.store.events(since_ms=0))
    assert "hunter2-secret" not in logtxt and "hunter2-secret" not in events
    assert "arg-secret-zzz" not in logtxt and "arg-secret-zzz" not in events        # argv0 + count only
    assert "spawn" in logtxt and "argv0=sleep" in logtxt
    v = client(d, "get", job=jid)
    assert "hunter2-secret" not in json.dumps(v) and v["env_keys"] == ["API_TOKEN"]
    assert v["command"] == ["sleep", "5", "arg-secret-zzz"]             # get returns the command to the same-user caller
    assert "command" not in client(d, "list")["jobs"][0]
    specfile = open(os.path.join(d.paths.job_dir(jid), "spec.json")).read()
    assert "hunter2-secret" not in specfile


def test_status_schema_keys(daemon, clock, plat):
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))
    run_ticks(daemon, clock, 3)
    st = client(daemon, "status")
    for k in ("version", "daemon_pid", "uptime_s", "enforce", "draining", "machine", "memory", "gpu", "cpu", "pressure",
              "protective", "sleep_prevented", "night_window", "running", "queue", "reservation", "projects",
              "offenders", "warnings"):
        assert k in st, k
    for k in ("budget_bytes", "budget_gib", "reserved_gib", "pending_gib", "reclaimable_gib", "live_free_gib",
              "reclaimable_parts_gib", "level", "swap_used_gib", "swap_total_gib", "swap_known"):
        assert k in st["memory"], k
    for k in ("chip", "memsize_bytes", "memsize_gib", "physical_cpu", "perf_cores", "eff_cores", "os", "gpu_wired_limit_mb"):
        assert k in st["machine"], k
    assert st["night_window"]["start"] == "23:00" and st["night_window"]["tz"] == "UTC" and "in_night" in st["night_window"]
    assert st["sleep_prevented"] is True and st["gpu"]["exclusive_job"] == jid
    q = st["queue"][0]
    assert q["blocked_by"] == ["gpu"] and q["position"] == 1 and q["why"]
    assert {p["id"] for p in st["projects"]} >= {"p", "q"}


def test_job_view_keys(daemon, clock):
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=jid)
    for k in ("job", "project", "name", "state", "held_reason", "queue", "priority", "checkpoint", "spawn", "resources",
              "runtime", "attempt", "resume", "submitted", "enqueued", "held", "started", "released", "blocked_by", "why",
              "position", "footprint_gib", "peak_footprint_gib", "flags", "exit", "pid", "command", "cwd", "env_keys",
              "logs", "after", "warnings"):
        assert k in v, k
    for k in ("spilled", "checkpoint_failed", "over_limit", "cancel_requested", "yield_requested", "estimate_overrun",
              "blocking_day"):
        assert k in v["flags"], k


def test_status_does_not_mutate(daemon, clock):
    client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))
    client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))
    run_ticks(daemon, clock, 3)

    def checksum():
        rows = [tuple(r) for r in daemon.store.conn.execute("select * from job order by id")]
        meta = [tuple(r) for r in daemon.store.conn.execute("select * from meta order by key")]
        proj = [tuple(r) for r in daemon.store.conn.execute("select * from project order by id")]
        return json.dumps([rows, meta, proj], default=str)

    before = checksum()
    ev_before = daemon.store.conn.execute("select count(*) from event").fetchone()[0]
    for _ in range(3):
        client(daemon, "status")
        client(daemon, "status", brief=True)
    assert checksum() == before


def test_sample_event_once_per_minute(daemon, clock):
    run_ticks(daemon, clock, 130)
    samples = daemon.store.events(kinds=["sample"], since_ms=0)
    assert 2 <= len(samples) <= 3
    b = samples[0]["body"]
    for k in ("level", "reclaimable_gib", "free_gib", "swap_used_gib", "budget_gib", "reserved_gib", "pending_gib",
              "gpu_util", "running"):
        assert k in b, k


def test_log_rotation_three_generations(tmp_path, clock):
    path = str(tmp_path / "gsched.log")
    log = DecisionLog(path, 2000, clock)
    for i in range(400):
        log.decision("admit", job=f"j{i}", note="x" * 40)
    log.close()
    names = sorted(os.listdir(tmp_path))
    assert names == ["gsched.log", "gsched.log.1", "gsched.log.2", "gsched.log.3"]
    assert os.path.getsize(path) <= 2200
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    first = open(path).readline()
    assert "admit job=" in first and "note=" in first


def test_log_line_format(tmp_path, clock):
    log = DecisionLog(str(tmp_path / "l.log"), 10**6, clock)
    log.decision("admit", job="J", project="has space", mem_bytes=20 * GiB, none=None, flag=True, ids=["a", "b"])
    line = open(tmp_path / "l.log").read().strip()
    assert " admit job=J project=\"has space\" mem=20.0GiB none=null flag=true ids=[a,b]" in line
    assert line[:4].isdigit() and "T" in line.split()[0]


def test_log_contains_decisions_not_secrets(make_daemon, clock):
    d = make_daemon(log_enabled=True)
    project(d, "p")
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 3)
    d.log.close()
    txt = open(d.paths.log_file).read()
    assert f"admit job={jid}" in txt and "daemon_start" in txt


def test_tick_slow_event_rate_limited(daemon, clock, monkeypatch):
    import gsched.tick as tk
    t = [0.0]

    def fake_pc():
        t[0] += 0.3
        return t[0]
    monkeypatch.setattr(tk.time, "perf_counter", fake_pc)
    run_ticks(daemon, clock, 5)
    evs = [e for e in daemon.store.events(since_ms=0) if e["kind"] == "tick_slow"]
    assert len(evs) == 1 and "total_ms" in evs[0]["body"]
    run_ticks(daemon, clock, 65)
    assert len([e for e in daemon.store.events(since_ms=0) if e["kind"] == "tick_slow"]) == 2


def test_config_reload_by_mtime_and_failure_keeps_old(make_daemon, clock):
    from gsched.config import Config
    d = make_daemon()
    d._cfg_given = False                       # use the file-backed path
    path = d.paths.config
    with open(path, "w") as fh:
        json.dump({"enforce": True, "timezone": "UTC", "grace_s": 77}, fh)
    run_ticks(d, clock, 2)
    assert d.cfg.grace_s == 77
    with open(path, "w") as fh:
        json.dump({"enforce": True, "grace_s": 0}, fh)
    os.utime(path, (clock.now_ms() / 1000 + 5, clock.now_ms() / 1000 + 5))
    run_ticks(d, clock, 2)
    assert d.cfg.grace_s == 77
    assert [e for e in d.store.events(since_ms=0) if e["kind"] == "config_reload_failed"]


def test_projects_reload_and_missing_project(daemon, clock):
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    j2 = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    os.unlink(os.path.join(daemon.paths.projects, "p.json"))
    daemon.reload_all()
    v = client(daemon, "get", job=j2)
    assert v["state"] == "queued" and v["blocked_by"] == ["project_missing"]
