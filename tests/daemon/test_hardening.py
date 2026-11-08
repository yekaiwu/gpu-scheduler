"""Hardening from the review (docs/implementation/16 section 3)."""

import os
import sqlite3

import pytest

from gsched.config import Config
from gsched.platform.fake import SIGKILL, SIGTERM
from tests.daemon.conftest import client, project, run_ticks, spec


def kinds(d, kind, job=None):
    return d.store.events(kinds=[kind], since_ms=0, job=job)


# ---- 1. tick exception guard ---------------------------------------------------------------------------------
def test_tick_exception_does_not_stop_daemon_and_skips_admission(daemon, clock, monkeypatch):
    def boom(now):
        raise RuntimeError("policy bug")
    monkeypatch.setattr(daemon.residents, "poll", boom)
    run_ticks(daemon, clock, 1)                                 # a full tick fails first; hint-driven ticks stay closed
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 3)                                 # the loop keeps running; nothing raises out of step()
    assert daemon.store.get(jid)["state"] == "queued"           # fail closed: no admission while a phase is failing
    errs = kinds(daemon, "tick_error")
    assert errs and errs[0]["body"]["phase"] == "residents" and "RuntimeError" in errs[0]["body"]["error"]
    assert "policy bug" in errs[0]["body"]["tb"]
    client(daemon, "ping")                                      # RPC still answers
    monkeypatch.undo()
    run_ticks(daemon, clock, 3)
    assert daemon.store.get(jid)["state"] == "held"


def test_tick_error_persistent_after_20(daemon, clock, monkeypatch):
    monkeypatch.setattr(daemon.ticker, "_bookkeep", lambda now: 1 / 0)
    run_ticks(daemon, clock, 19)
    assert not kinds(daemon, "tick_error_persistent")
    run_ticks(daemon, clock, 1)
    p = kinds(daemon, "tick_error_persistent")
    assert len(p) == 1 and p[0]["body"]["phase"] == "bookkeep" and p[0]["body"]["ticks"] == 20
    assert len(kinds(daemon, "tick_error")) < 20                # a persistent bug does not write a row per tick
    st = client(daemon, "status")
    assert "tick_error_persistent" in [w["kind"] for w in st["warnings"]]
    monkeypatch.undo()
    run_ticks(daemon, clock, 2)
    assert daemon.ticker.fail_streak == {}


def test_sqlite_operational_error_is_guarded(daemon, clock, monkeypatch):
    def full(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(daemon.store, "commit_admit", full)
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 3)
    assert daemon.store.get(jid)["state"] == "queued"
    assert kinds(daemon, "tick_error")[0]["body"]["phase"] == "admit"
    monkeypatch.undo()
    run_ticks(daemon, clock, 2)
    assert daemon.store.get(jid)["state"] == "held"


def test_planner_failure_skips_signals_and_admission_but_loop_survives(daemon, clock, monkeypatch):
    from gsched.policy import maintenance
    monkeypatch.setattr(maintenance, "plan", lambda snap, cfg: (_ for _ in ()).throw(ValueError("x")))
    run_ticks(daemon, clock, 2)
    assert kinds(daemon, "tick_error")[0]["body"]["phase"] == "maintenance"


def test_rpc_handler_exception_is_isolated(daemon, monkeypatch):
    monkeypatch.setattr(daemon.handlers, "m_ping", lambda *a: 1 / 0)
    r = client(daemon, "ping", raw=True)
    assert r["ok"] is False and r["error"]["code"] == "internal"
    monkeypatch.undo()
    assert client(daemon, "ping")["pid"] == os.getpid()


def test_serve_forever_survives_a_step_exception(daemon, monkeypatch):
    calls = []

    def step(timeout=0.0):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("selector callback bug")
        daemon.stopping = True
    monkeypatch.setattr(daemon, "step", step)
    daemon.serve_forever()
    assert len(calls) == 2 and kinds(daemon, "loop_error")


# ---- 2. job log cap --------------------------------------------------------------------------------------------
@pytest.fixture
def logdaemon(make_daemon):
    d = make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", night_window=None,
                                              job_log_max_bytes=2048, job_log_keep_bytes=1024))
    project(d, "p")
    return d


def test_job_log_cap_truncates_keeping_tail(logdaemon, clock, plat):
    d = logdaemon
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 2)
    w = plat.workload(jid)
    jd = d.paths.job_dir(jid)
    out = os.path.join(jd, "stdout.log")
    w.write_stdout("a" * 1500 + "b" * 1500)                      # 3000 > 2048
    run_ticks(d, clock, 11)
    assert os.path.getsize(out) == 0
    kept = open(out + ".1").read()
    assert len(kept) == 1024 and kept == "b" * 1024
    ev = kinds(d, "job_log_truncated", jid)
    assert ev and ev[0]["body"]["stream"] == "stdout" and ev[0]["body"]["size_bytes"] == 3000
    w.write_stdout("after")                                      # the O_APPEND writer continues at the new end
    assert open(out).read() == "after"
    assert oct(os.stat(out + ".1").st_mode & 0o777) == "0o600"
    # under the cap: untouched; and a second check inside 10 s is skipped
    w.write_stdout("c" * 100)
    run_ticks(d, clock, 11)
    assert os.path.getsize(out) == 105 and len(kinds(d, "job_log_truncated", jid)) == 1


def test_job_log_cap_ignores_custom_paths_and_external_jobs(logdaemon, clock, plat, short_tmp):
    d = logdaemon
    mine = str(short_tmp / "mine.log")
    jid = client(d, "submit", spec=spec(stdout=mine))["job"]
    run_ticks(d, clock, 2)
    with open(mine, "w") as fh:
        fh.write("x" * 5000)
    run_ticks(d, clock, 12)
    assert os.path.getsize(mine) == 5000 and not kinds(d, "job_log_truncated")


# ---- 3. sleep and clock-jump safe timers -----------------------------------------------------------------------
def stopping_job(d, clock, plat):
    jid = client(d, "submit", spec=spec(checkpoint="restartable", runtime={"estimate_s": 60}))["job"]
    run_ticks(d, clock, 2)
    plat.workload(jid).on_signal(SIGTERM, "ignore")
    client(d, "cancel", job=jid)
    run_ticks(d, clock, 1)
    assert d.store.get(jid)["held_reason"] == "stopping"
    return jid


def test_forward_clock_jump_does_not_fire_kill_timer(daemon, clock, plat):
    jid = stopping_job(daemon, clock, plat)
    req0 = daemon.store.get(jid)["stop_requested_ms"]
    clock.advance(5)
    clock.set_wall(clock.now_ms() + 3600 * 1000)                 # the Mac slept for an hour
    daemon.step(0)
    assert not [s for s in plat.signals if s[1] == SIGKILL]      # the 15 s kill grace has NOT silently expired
    ev = kinds(daemon, "clock_skew")
    assert len(ev) == 1 and 3590_000 < ev[0]["body"]["skew_ms"] < 3610_000
    shifted = daemon.store.get(jid)["stop_requested_ms"]
    assert shifted - req0 > 3590_000 and shifted <= clock.now_ms()
    run_ticks(daemon, clock, 16)                                 # real time passes: now it escalates
    assert [s for s in plat.signals if s[1] == SIGKILL]


def test_backward_clock_jump_shifts_timers_back_clamped(daemon, clock, plat):
    jid = stopping_job(daemon, clock, plat)
    req0 = daemon.store.get(jid)["stop_requested_ms"]
    clock.advance(2)
    clock.set_wall(clock.now_ms() - 3600 * 1000)
    daemon.step(0)
    assert kinds(daemon, "clock_skew")[0]["body"]["skew_ms"] < -3590_000
    after = daemon.store.get(jid)["stop_requested_ms"]
    assert after < req0 and after <= clock.now_ms()               # never lands in the future


def test_small_clock_wobble_is_not_skew(daemon, clock, plat):
    stopping_job(daemon, clock, plat)
    clock.advance(1)
    clock.set_wall(clock.now_ms() + 20_000)
    daemon.step(0)
    assert not kinds(daemon, "clock_skew")


# ---- 4. lsof off the tick -----------------------------------------------------------------------------------------
@pytest.fixture
def pdaemon(make_daemon):
    return make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", heavy_listen_ports=(8089,),
                                                 port_probe_s=1))


def test_lsof_runs_off_the_tick_and_attaches_pid_later(pdaemon, clock, plat, monkeypatch):
    d = pdaemon
    monkeypatch.setattr(plat, "port_owner_pids", lambda port: pytest.fail("the tick must not run lsof inline"))
    plat.port_lookup_delay_s = 2.0
    pid = plat.add_foreign("mystery", 0.2, port=8089)
    run_ticks(d, clock, 1)
    off = [o for o in d.store.offender_open() if o["port"] == 8089]
    assert len(off) == 1 and off[0]["pid"] is None               # no result yet, and the tick did not wait for it
    assert 8089 in d.scanner._lookups
    run_ticks(d, clock, 2)
    off = [o for o in d.store.offender_open() if o["port"] == 8089]
    assert off[0]["pid"] == pid and 8089 not in d.scanner._lookups


def test_lsof_hung_lookup_is_killed_after_3s(pdaemon, clock, plat):
    d = pdaemon
    plat.port_lookup_delay_s = 3600.0
    plat.add_foreign("mystery", 0.2, port=8089)
    run_ticks(d, clock, 2)
    assert 8089 in d.scanner._lookups
    run_ticks(d, clock, 3)
    assert 8089 not in d.scanner._lookups and any(a[0] == "lsof" for a in plat.async_killed)
    assert [o for o in d.store.offender_open() if o["port"] == 8089][0]["pid"] is None
