import json
import os
import time

import pytest

from gsched.config import Config
from gsched.platform.fake import SIGTERM
from tests.residents.conftest import ClientError, client, project, pump_steps, run_ticks, spec, write_profile


def row(d, jid):
    return d.store.get(jid)


def live_job(d, name):
    r = d.store.live_profile_row(name)
    return r


def test_ensure_inserts_once_and_refreshes_touch(rd, clock):
    write_profile(rd)
    a = client(rd, "ensure", profile="llama")
    assert a["view"] in ("queued", "starting") and a["job"]
    t0 = row(rd, a["job"])["last_touch_ms"]
    clock.advance(30)
    b = client(rd, "ensure", profile="llama")
    assert b["job"] == a["job"]
    assert row(rd, a["job"])["last_touch_ms"] > t0
    assert rd.store.conn.execute("select count(*) from job").fetchone()[0] == 1


def test_ensure_never_spawns_on_call(rd, plat):
    write_profile(rd)
    out = rd.handlers.m_ensure(None, {"profile": "llama"}, "cli")        # the handler itself, no ticks
    assert out["view"] == "queued" and plat.spawned == []
    assert row(rd, out["job"])["state"] == "queued"


def test_ensure_runs_after_tick_and_reports_running(rd, clock, plat):
    write_profile(rd)
    j = client(rd, "ensure", profile="llama")["job"]
    run_ticks(rd, clock, 3)
    v = client(rd, "ensure", profile="llama")
    assert v["view"] == "running" and v["ready"]          # no health_url: ready at once
    assert row(rd, j)["held_reason"] == "live"


def test_ensure_observe_mode(make_daemon, clock, plat):
    d = make_daemon(Config.defaults().replace(timezone="UTC"))
    project(d, "local-llm")
    write_profile(d)
    out = client(d, "ensure", profile="llama")
    assert out["view"] == "observe" and out["would"]["would"] == "admit_now"
    assert d.store.conn.execute("select count(*) from job").fetchone()[0] == 0
    run_ticks(d, clock, 3)
    assert plat.spawned == []


def test_ensure_refused_and_unknown(rd):
    write_profile(rd, admit=False, reason="nope")
    assert client(rd, "ensure", profile="llama") == {"profile": "llama", "view": "refused", "reason": "nope"}
    with pytest.raises(ClientError) as e:
        client(rd, "ensure", profile="ghost")
    assert e.value.code == "unknown_profile"


def test_profile_forces_restartable_day_daemon(rd, clock):
    write_profile(rd, spec={"command": ["sleep", "5"], "cwd": "/tmp", "queue": "night", "spawn": "external",
                            "resources": {"memory_gib": 4, "gpu": "none"}}, listen_port=8081)
    j = client(rd, "ensure", profile="llama")["job"]
    r = row(rd, j)
    assert (r["checkpoint"], r["queue"], r["spawn"]) == ("restartable", "day", "daemon")
    assert "port:8081" in json.loads(r["locks_json"]) and r["listen_port"] == 8081 and r["profile"] == "llama"
    write_profile(rd, name="bad", spec={"command": ["sleep", "5"], "checkpoint": "atomic", "cwd": "/tmp",
                                        "resources": {"memory_gib": 4}})
    assert "restartable" in rd.residents.registry.errors["bad"]
    with pytest.raises(ClientError) as e:
        client(rd, "ensure", profile="bad")
    assert e.value.code == "unknown_profile"


def test_profile_env_expansion_fails_closed(rd, monkeypatch):
    write_profile(rd, name="envp", spec={"command": ["${GS_TEST_ROOT}/srv"], "cwd": "/tmp",
                                         "resources": {"memory_gib": 4}})
    assert "GS_TEST_ROOT" in rd.residents.registry.errors["envp"]
    with open(rd.paths.env_file, "w") as fh:
        fh.write("# comment\nGS_TEST_ROOT=/bin\n")
    rd.reload_all()
    assert "envp" in rd.residents.registry.profiles
    assert rd.residents.registry.profiles["envp"].spec.command == ("/bin/srv",)
    monkeypatch.setenv("GS_OTHER", "/usr/bin")
    write_profile(rd, name="envq", spec={"command": ["${GS_OTHER}/x"], "cwd": "/tmp", "resources": {"memory_gib": 1}})
    assert rd.residents.registry.profiles["envq"].spec.command == ("/usr/bin/x",)


@pytest.mark.parametrize("url", ["http://example.com:8081/health", "https://127.0.0.1:8081/h", "http://10.0.0.5:80/",
                                 "http://127.0.0.1.evil.com:80/"])
def test_profile_non_loopback_url_rejected(rd, url):
    write_profile(rd, health_url=url)
    assert "loopback" in rd.residents.registry.errors["llama"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8081/health", "http://localhost:8081/health",
                                 "http://[::1]:8081/health"])
def test_profile_loopback_urls_accepted(rd, url):
    write_profile(rd, health_url=url)
    assert "llama" in rd.residents.registry.profiles


def test_ready_after_health_2xx(rd, clock, plat, http):
    write_profile(rd, health_url=f"http://127.0.0.1:{http.port}/health")
    j = client(rd, "ensure", profile="llama")["job"]
    http.route("/health", 503)
    pump_steps(rd, clock, http, 5)
    assert row(rd, j)["held_reason"] == "live" and row(rd, j)["ready"] == 0
    assert client(rd, "ensure", profile="llama")["view"] == "starting"
    http.route("/health", 200, "ok")
    pump_steps(rd, clock, http, 6)
    assert row(rd, j)["ready"] == 1 and j in rd.residents.baselines
    assert client(rd, "ensure", profile="llama")["view"] == "running"


def test_health_probe_nonblocking_timeout(rd, clock, plat, http):
    write_profile(rd, health_url=f"http://127.0.0.1:{http.port}/health")
    j = client(rd, "ensure", profile="llama")["job"]
    http.hang = True
    t0 = time.monotonic()
    pump_steps(rd, clock, http, 6, advance=0.6)         # the server never answers
    assert time.monotonic() - t0 < 2.0                  # nothing blocked the loop
    assert row(rd, j)["ready"] == 0
    assert not rd.residents.probes or all(p.started_mono >= clock.mono() - 1.0 for p in rd.residents.probes.values())


def started_ready(rd, clock, http, **prof):
    http.route("/health", 200)
    write_profile(rd, health_url=f"http://127.0.0.1:{http.port}/health",
                  slots_url=f"http://127.0.0.1:{http.port}/slots", **prof)
    j = client(rd, "ensure", profile="llama")["job"]
    pump_steps(rd, clock, http, 5)
    assert row(rd, j)["ready"] == 1
    return j


def test_idle_unload_waits_for_slots_then_terminates(rd, clock, plat, http):
    j = started_ready(rd, clock, http, idle_unload_s=100)
    http.route("/slots", 200, json.dumps([{"id": 0, "is_processing": True}]))
    pump_steps(rd, clock, http, 105)
    assert plat.signals == []                                    # busy: no signal
    assert j in rd.residents.stop_pending
    http.route("/slots", 200, json.dumps([{"id": 0, "is_processing": False}]))
    pump_steps(rd, clock, http, 6)
    assert [s[1] for s in plat.signals if s[1] == SIGTERM]
    pump_steps(rd, clock, http, 3)
    r = row(rd, j)
    assert r["state"] == "done" and r["end_reason"] == "idle_unload"
    # ensure creates a fresh row next time
    assert client(rd, "ensure", profile="llama")["job"] != j


def test_slots_error_waits_grace(rd, clock, plat, http):
    j = started_ready(rd, clock, http, idle_unload_s=100)
    http.route("/slots", 500)
    pump_steps(rd, clock, http, 110)
    assert plat.signals == [] and j in rd.residents.stop_pending
    assert [e for e in rd.store.events(job=j, since_ms=0) if e["kind"] == "slots_unavailable"]
    pump_steps(rd, clock, http, 190)                             # preempt_grace_ms = 180 s from the first request
    assert [s for s in plat.signals if s[1] == SIGTERM]
    pump_steps(rd, clock, http, 3)
    assert row(rd, j)["state"] == "done"


def test_idle_unload_skipped_when_hold(rd, clock, plat, http):
    j = started_ready(rd, clock, http, idle_unload_s=50)
    assert client(rd, "hold", profile="llama") == {"profile": "llama", "hold": True}
    assert row(rd, j)["hold"] == 1
    http.route("/slots", 200, "[]")
    pump_steps(rd, clock, http, 120)
    assert plat.signals == [] and row(rd, j)["held_reason"] == "live"
    client(rd, "unhold", profile="llama")
    pump_steps(rd, clock, http, 10)
    assert [s for s in plat.signals if s[1] == SIGTERM]


def test_touch_by_ensure_defers_idle_unload(rd, clock, plat, http):
    j = started_ready(rd, clock, http, idle_unload_s=100)
    http.route("/slots", 200, "[]")
    for _ in range(4):
        pump_steps(rd, clock, http, 60)
        client(rd, "ensure", profile="llama")
    assert plat.signals == []


def test_same_project_switch_after_min_hold(rd, clock, plat):
    write_profile(rd, name="a")
    write_profile(rd, name="b")
    ja = client(rd, "ensure", profile="a")["job"]
    run_ticks(rd, clock, 3)
    jb = client(rd, "ensure", profile="b")["job"]
    assert row(rd, ja)["switch_to"] == "b"
    run_ticks(rd, clock, 10)
    assert plat.signals == [] and row(rd, ja)["held_reason"] == "live"       # a has not run restartable_min_hold_s
    run_ticks(rd, clock, 60)
    assert [s for s in plat.signals if s[1] == SIGTERM]
    run_ticks(rd, clock, 4)
    assert row(rd, jb)["state"] == "held"
    assert row(rd, ja)["state"] in ("queued",) and row(rd, ja)["switch_to"] is None


def test_switch_revert_cancels_pending(rd, clock, plat):
    write_profile(rd, name="a", spec={"command": ["sleep", "1"], "cwd": "/tmp", "resources": {"memory_gib": 30, "gpu": "exclusive"}})
    write_profile(rd, name="b", spec={"command": ["sleep", "1"], "cwd": "/tmp", "resources": {"memory_gib": 30, "gpu": "exclusive"}})
    ja = client(rd, "ensure", profile="a")["job"]
    run_ticks(rd, clock, 3)
    jb = client(rd, "ensure", profile="b")["job"]
    run_ticks(rd, clock, 3)
    assert row(rd, jb)["state"] == "queued" and row(rd, ja)["switch_to"] == "b"
    client(rd, "ensure", profile="a")                          # last caller wins
    assert row(rd, ja)["switch_to"] is None
    assert row(rd, jb)["state"] == "cancelled" and row(rd, jb)["end_reason"] == "switch_reverted"
    run_ticks(rd, clock, 80)
    assert plat.signals == []


def test_different_project_ensure_does_not_set_switch(rd, clock):
    project(rd, "other")
    write_profile(rd, name="a")
    write_profile(rd, name="c", project="other")
    ja = client(rd, "ensure", profile="a")["job"]
    run_ticks(rd, clock, 3)
    client(rd, "ensure", profile="c")
    assert row(rd, ja)["switch_to"] is None


def test_port_lock_prevents_overlap(rd, clock, plat):
    small = {"command": ["sleep", "1"], "cwd": "/tmp", "resources": {"memory_gib": 2, "gpu": "none"}}
    write_profile(rd, name="a", spec=small, listen_port=8081)
    write_profile(rd, name="b", spec=small, listen_port=8081)
    ja = client(rd, "ensure", profile="a")["job"]
    jb = client(rd, "ensure", profile="b")["job"]
    run_ticks(rd, clock, 4)
    held = [j for j in (ja, jb) if row(rd, j)["state"] == "held"]
    assert held == [ja]
    v = client(rd, "get", job=jb)
    assert v["blocked_by"] == ["lock:port:8081"]


def test_resident_listen_port_is_not_foreign(make_daemon, clock, plat):
    d = make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", heavy_listen_ports=(8081,)))
    project(d, "local-llm")
    write_profile(d, listen_port=8081)
    j = client(d, "ensure", profile="llama")["job"]
    run_ticks(d, clock, 3)
    plat.workload(j).listen_port = 8081
    run_ticks(d, clock, 8)
    assert not d.protective.active and plat.signals == []


def test_abort_line_unloads_after_ready_only(rd, clock, plat, http):
    http.route("/health", 503)
    write_profile(rd, health_url=f"http://127.0.0.1:{http.port}/health", abort_level_below=40)
    j = client(rd, "ensure", profile="llama")["job"]
    plat.set_mem(level=30)
    pump_steps(rd, clock, http, 6)
    assert plat.signals == []                        # not ready yet: the abort line is not armed
    http.route("/health", 200)
    plat.set_mem(level=80)
    pump_steps(rd, clock, http, 4)
    assert row(rd, j)["ready"] == 1
    plat.set_mem(level=30)
    pump_steps(rd, clock, http, 4)
    assert [s for s in plat.signals if s[1] == SIGTERM]
    assert [e for e in rd.store.events(job=j, since_ms=0) if e["kind"] == "abort_tripped"]


def test_inhibit_blocks_ensure(rd, clock):
    write_profile(rd)
    assert client(rd, "inhibit", profile="llama") == {"profile": "llama", "inhibited": True}
    assert client(rd, "ensure", profile="llama")["view"] == "inhibited"
    assert rd.store.conn.execute("select count(*) from job").fetchone()[0] == 0
    assert rd.store.meta_get("inhibit:llama") == "1"
    client(rd, "uninhibit", profile="llama")
    assert client(rd, "ensure", profile="llama")["view"] in ("queued", "starting")


def test_inhibit_makes_queued_row_ineligible(rd, clock):
    small = {"command": ["sleep", "1"], "cwd": "/tmp", "resources": {"memory_gib": 38, "gpu": "none"}}
    write_profile(rd, spec=small)
    blocker = client(rd, "submit", spec=spec(resources={"memory_gib": 20, "gpu": "none"}))["job"]
    run_ticks(rd, clock, 2)
    j = client(rd, "ensure", profile="llama")["job"]
    client(rd, "inhibit", profile="llama")
    v = client(rd, "get", job=j)
    assert v["blocked_by"] == ["inhibited"]


def test_bearer_never_logged(make_daemon, clock, plat, http):
    d = make_daemon(log_enabled=True)
    project(d, "local-llm")
    secret = "s3cr3t-token-xyz"
    bf = os.path.join(str(d.paths.home), "bearer.txt")
    with open(bf, "w") as fh:
        fh.write(secret + "\n")
    http.route("/health", 200)
    write_profile(d, health_url=f"http://127.0.0.1:{http.port}/health", health_bearer_file=bf)
    client(d, "ensure", profile="llama")
    pump_steps(d, clock, http, 6)
    assert any(f"Bearer {secret}" in r for r in http.requests)            # it was sent ...
    d.log.close()
    logtxt = open(d.paths.log_file).read()
    events = json.dumps(d.store.events(since_ms=0))
    dump = json.dumps([dict(r) for r in d.store.conn.execute("select * from job")], default=str)
    assert secret not in logtxt and secret not in events and secret not in dump       # ... and never stored


def test_status_lists_profiles(rd):
    write_profile(rd)
    st = client(rd, "status")
    assert st["profiles"][0]["name"] == "llama"


def test_ready_marker_requeues_same_id(daemon, clock, plat):
    from gsched.platform.fake import SIGUSR1
    from tests.policy.builders import ms
    clock.set_wall(ms("2026-11-08T23:30:00Z"))
    jid = client(daemon, "submit", spec=spec(checkpoint="cooperative", runtime={"estimate_s": 20000}))["job"]
    run_ticks(daemon, clock, 3)
    plat.workload(jid).on_signal(SIGUSR1, "checkpoint_and_exit")
    clock.set_wall(ms("2026-11-09T08:00:05Z"))
    run_ticks(daemon, clock, 3)
    r = row(daemon, jid)
    assert r["state"] == "queued" and r["attempt"] == 2 and r["id"] == jid
