import json
import os
import sqlite3

import pytest

import gsched.supervise as sv
from gsched.platform.fake import SIGKILL, SIGTERM
from tests.daemon.conftest import ClientError, client, project, run_ticks, spec


def get(d, jid):
    return client(d, "get", job=jid)


def row(d, jid):
    return d.store.get(jid)


def submit_run(d, clock, **over):
    jid = client(d, "submit", spec=spec(**over))["job"]
    run_ticks(d, clock, 2)
    return jid, d.platform.workload(jid)


def test_attach_before_gate_release(daemon, clock, monkeypatch):
    """The gate byte is written only after begin_child committed (checked from a second connection)."""
    observed = []
    real_os = os

    class OsProxy:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def write(self, fd, data):
            if data == b"1":
                db = sqlite3.connect(daemon.paths.db)
                observed.append(db.execute("SELECT pid, gate_released, held_reason FROM job").fetchone())
                db.close()
            return real_os.write(fd, data)

    monkeypatch.setattr(sv, "os", OsProxy())
    jid, w = submit_run(daemon, clock)
    assert observed and observed[0][0] == w.pid and observed[0][1] == 0 and observed[0][2] == "spawning"
    assert row(daemon, jid)["gate_released"] == 1


def test_spawn_error_marks_failed_slot_free(daemon, clock, plat):
    plat.spawn_error = OSError(2, "nope")
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    v = get(daemon, jid)
    assert v["state"] == "failed" and v["exit"]["end_reason"] == "spawn_error"
    plat.spawn_error = None
    j2 = client(daemon, "submit", spec=spec(resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    assert get(daemon, j2)["state"] == "held"


def test_report_timeout_aborts_and_requeues(daemon, clock, plat):
    plat.suppress_report = True
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["held_reason"] == "spawning" and row(daemon, jid)["gate_released"] == 0
    run_ticks(daemon, clock, 11)
    evs = [e["kind"] for e in daemon.store.events(job=jid, since_ms=0)]
    assert "spawn_abort" in evs
    r = row(daemon, jid)
    assert r["gate_released"] == 0 and r["state"] in ("queued", "held") and r["attempt"] == 1
    assert plat.signals == []        # nothing was exec'd, nothing was signaled
    plat.suppress_report = False
    run_ticks(daemon, clock, 15)
    assert row(daemon, jid)["held_reason"] == "live"


def test_report_timeout_repeats_then_fails(daemon, clock, plat):
    plat.suppress_report = True
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 60)
    r = row(daemon, jid)
    assert r["state"] == "failed" and r["end_reason"] == "spawn_error"


def test_gate_write_failure_requeues_unstarted(daemon, clock, plat, monkeypatch):
    real_os = os

    class OsProxy:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def write(self, fd, data):
            if data == b"1":
                raise OSError(32, "broken pipe")
            return real_os.write(fd, data)

    monkeypatch.setattr(sv, "os", OsProxy())
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 2)               # tick admits and spawns; the next step reads the report and fails to write
    monkeypatch.undo()
    aborts = [e for e in daemon.store.events(job=jid, since_ms=0) if e["kind"] == "spawn_abort"]
    assert aborts and aborts[0]["body"]["reason"] == "gate_write_failed"
    assert row(daemon, jid)["gate_released"] == 0
    run_ticks(daemon, clock, 4)               # the gate closed, exit 111, requeued unstarted, then ran
    r = row(daemon, jid)
    assert r["held_reason"] == "live" and r["attempt"] == 1
    assert plat.signals == []


def test_exit_111_unreleased_is_requeue_not_failure(daemon, clock, plat):
    plat.suppress_report = True
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 1)
    w = plat.workload(jid)
    w.exit(111)                       # gate never opened
    plat.suppress_report = False
    run_ticks(daemon, clock, 1)
    r = row(daemon, jid)
    assert r["state"] != "failed"


def test_exit_111_after_release_is_failure(daemon, clock):
    jid, w = submit_run(daemon, clock)
    w.exit(111)
    run_ticks(daemon, clock, 2)
    v = get(daemon, jid)
    assert v["state"] == "failed" and v["exit"]["code"] == 111


@pytest.mark.parametrize("code,sig,state,reason", [(0, None, "done", "exit"), (7, None, "failed", "exit"),
                                                   (None, 11, "failed", "signal")])
def test_exit_json_finalizes(daemon, clock, code, sig, state, reason):
    jid, w = submit_run(daemon, clock)
    w.set_footprint(2)
    w.exit(code) if sig is None else w.die(sig)
    run_ticks(daemon, clock, 2)
    v = get(daemon, jid)
    assert v["state"] == state and v["exit"]["end_reason"] == reason
    assert v["exit"]["code"] == code and v["exit"]["signal"] == sig
    assert os.path.exists(os.path.join(daemon.paths.job_dir(jid), "exit.1.json"))
    assert not os.path.exists(os.path.join(daemon.paths.job_dir(jid), "exit.json"))
    h = daemon.store.conn.execute("select outcome, runtime_s from history where job=?", (jid,)).fetchone()
    assert h["outcome"] == state


def test_missing_exit_json_is_lost(daemon, clock):
    jid, w = submit_run(daemon, clock)
    w.exit(0, write_record=False)
    run_ticks(daemon, clock, 2)
    v = get(daemon, jid)
    assert v["state"] == "lost" and v["exit"]["end_reason"] == "exit_unknown"


def test_identity_mismatch_lost_no_signal(daemon, clock, plat):
    jid, w = submit_run(daemon, clock)
    plat.recycle_pid(w.pid)
    run_ticks(daemon, clock, 2)
    v = get(daemon, jid)
    assert v["state"] == "lost" and v["exit"]["end_reason"] == "identity_mismatch"
    assert plat.signals == []


def test_pid_path_refreshed_once_after_exec(daemon, clock, plat):
    jid = client(daemon, "submit", spec=spec(command=["/bin/sleep", "5"]))["job"]
    run_ticks(daemon, clock, 1)
    r = row(daemon, jid)
    assert r["gate_released"] == 1 and "python" in r["pid_path"] or r["pid_path"] == "/bin/sleep"
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["pid_path"] == "/bin/sleep"
    # identity checks now use the post-exec path
    assert plat.identity_matches(daemon.supervisor.workload_ident(row(daemon, jid)))


def test_job_footprint_sums_tree_including_setsid_child(daemon, clock):
    jid, w = submit_run(daemon, clock)
    w.set_footprint(3)
    w.spawn_child("helper", 2)
    w.spawn_child("escapee", 4, setsid=True)
    run_ticks(daemon, clock, 2)
    v = get(daemon, jid)
    assert v["footprint_gib"] == 9.0
    assert row(daemon, jid)["peak_footprint_bytes"] == 9 * 2**30
    st = client(daemon, "status")
    assert st["memory"]["reserved_gib"] == 9.0      # max(reservation 1, footprint 9)


def test_descendant_llama_server_is_ours_not_foreign(daemon, clock):
    jid, w = submit_run(daemon, clock, checkpoint="restartable")
    w.spawn_child("llama-server", 12, path="/opt/homebrew/bin/llama-server")
    run_ticks(daemon, clock, 3)
    st = client(daemon, "status")
    assert not st["protective"] and st["offenders"] == []


def test_peak_footprint_recorded_in_history(daemon, clock):
    jid, w = submit_run(daemon, clock)
    w.set_footprint(5)
    run_ticks(daemon, clock, 2)
    w.set_footprint(1)
    run_ticks(daemon, clock, 1)
    w.exit(0)
    run_ticks(daemon, clock, 2)
    h = daemon.store.conn.execute("select peak_footprint_bytes from history where job=?", (jid,)).fetchone()
    assert h[0] == 5 * 2**30


def test_caffeinate_started_per_job(daemon, clock, plat):
    jid, w = submit_run(daemon, clock)
    assert len(plat.caffeinated) == 1


def test_reconcile_after_restart_finalizes_from_exit_json(make_daemon, clock, plat):
    d = make_daemon()
    project(d, "p")
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 2)
    w = plat.workload(jid)
    d.stop()                           # jobs keep running under their shims
    w.exit(0)                          # finishes while no daemon is up
    d2 = make_daemon()
    assert row(d2, jid)["state"] == "done"
    assert plat.signals == []


def test_reconcile_keeps_live_job(make_daemon, clock, plat):
    d = make_daemon()
    project(d, "p")
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 2)
    d.stop()
    d2 = make_daemon()
    assert row(d2, jid)["state"] == "held" and d2.reconcile_summary["kept"] == 1
    plat.workload(jid).exit(3)
    run_ticks(d2, clock, 2)
    assert client(d2, "get", job=jid)["exit"]["code"] == 3


def test_reconcile_shim_lost_workload_alive(make_daemon, clock, plat):
    d = make_daemon()
    project(d, "p")
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 2)
    d.stop()
    plat.kill_shim(jid)
    d2 = make_daemon()
    r = row(d2, jid)
    assert r["state"] == "held" and r["shim_pid"] is None
    plat.workload(jid).exit(0, write_record=False)
    run_ticks(d2, clock, 2)
    assert client(d2, "get", job=jid)["state"] == "lost"


def test_reconcile_unstarted_spawning_row(make_daemon, clock, plat):
    d = make_daemon()
    project(d, "p")
    plat.suppress_report = True
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 1)
    assert row(d, jid)["held_reason"] == "spawning"
    d.stop()                    # the gate write end dies with the old daemon: the workload exits 111
    plat.suppress_report = False
    plat.reap()
    d2 = make_daemon()
    run_ticks(d2, clock, 3)
    r = row(d2, jid)
    assert r["state"] in ("queued", "held") and r["attempt"] == 1


# ---- review regressions (2026-11-08) ---------------------------------------------------------------------------------
def test_reexec_after_path_refresh_is_same_process_not_lost(daemon, clock, plat):
    """A wrapper that `exec`s the real binary after the first path refresh keeps pid + start time: same process."""
    jid = client(daemon, "submit", spec=spec(command=["/bin/sh", "-c", "sleep 2; exec llama-server"],
                                             checkpoint="restartable"))["job"]
    run_ticks(daemon, clock, 3)
    assert row(daemon, jid)["pid_path"] == "/bin/sh"
    w = plat.workload(jid)
    w.rec.path = "/opt/fake/bin/llama-server"            # exec: same pid and start time, new executable
    run_ticks(daemon, clock, 2)
    r = row(daemon, jid)
    assert r["state"] == "held" and r["held_reason"] == "live"
    assert r["pid_path"] == "/opt/fake/bin/llama-server"
    assert [e for e in daemon.store.events(kinds=["pid_path_changed"]) if e["job"] == jid]
    assert not any(o.path == "/opt/fake/bin/llama-server" for o in daemon.last_scan.foreign_heavy)   # ours, not foreign
    assert not daemon.protective.active
    # ... and it is still stoppable: the identity used for signals follows the new path
    client(daemon, "cancel", job=jid)
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["state"] == "cancelled"
    assert any(s[1] == SIGTERM for s in plat.signals)


def test_reexec_does_not_mask_pid_reuse(daemon, clock, plat):
    jid, w = submit_run(daemon, clock)
    run_ticks(daemon, clock, 1)
    w.rec.path = "/opt/fake/bin/other"
    plat.recycle_pid(w.pid)                              # a different process: new start time
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["state"] == "lost" and row(daemon, jid)["end_reason"] == "identity_mismatch"
    assert plat.signals == []


def test_shim_death_while_daemon_runs_keeps_live_workload(daemon, clock, plat):
    """The shim is killed (kill -9, jetsam) while its workload keeps running: the job stays held (its memory is still
    in use and it can still be stopped), as reconcile does after a restart. When the workload goes: lost."""
    jid, w = submit_run(daemon, clock, resources={"memory_gib": 15})
    plat.kill_shim(jid)
    run_ticks(daemon, clock, 3)
    r = row(daemon, jid)
    assert r["state"] == "held" and r["held_reason"] == "live" and r["shim_pid"] is None
    assert [e for e in daemon.store.events(kinds=["shim_lost"]) if e["job"] == jid]
    from gsched.policy.fit import ledger
    assert ledger(daemon.snapshot()).reserved_bytes >= 15 * 2**30      # its memory still counts
    w.exit(0, write_record=False)
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["state"] == "lost" and row(daemon, jid)["end_reason"] == "exit_unknown"


def test_shim_lost_workload_can_still_be_terminated(daemon, clock, plat):
    jid, w = submit_run(daemon, clock)
    plat.kill_shim(jid)
    run_ticks(daemon, clock, 2)
    client(daemon, "cancel", job=jid, terminate=True)
    run_ticks(daemon, clock, 2)
    assert any(s[1] == SIGTERM for s in plat.signals)
    assert row(daemon, jid)["state"] in ("cancelled", "lost")


@pytest.mark.parametrize("rec", [
    {"exit_code": "x", "peak_footprint_bytes": "y"},
    {"exit_code": 0, "peak_footprint_bytes": "y"},
    {"signal": [9]},
    {"exit_code": 3.5},
])
def test_exit_json_with_bad_types_does_not_crash_the_tick(daemon, clock, rec):
    jid, w = submit_run(daemon, clock, checkpoint="restartable")
    path = daemon.supervisor.exit_file(jid)
    w.exit(0)
    with open(path, "w") as fh:
        json.dump({"v": 1, "job": jid, **rec}, fh)
    run_ticks(daemon, clock, 2)                           # used to raise TypeError in every tick and every restart
    st = row(daemon, jid)["state"]
    if isinstance(rec.get("exit_code"), int):
        assert st == "done"
    else:
        assert st == "lost"


def test_policy_stop_never_reaches_atomic_or_cooperative(daemon, clock, plat):
    """Defense in depth for ground rule 2: even if a planner put a non-restartable job in plan.stop, nothing is sent."""
    a, _ = submit_run(daemon, clock)
    c, _ = submit_run(daemon, clock, checkpoint="cooperative")
    for jid in (a, c):
        for reason in ("preempt", "morning", "pressure", "protective", "limit", "idle", "drain", "abort"):
            assert daemon.residents.request_unload(jid, reason) is False
            assert daemon.supervisor.stop(jid, reason) is False
        assert row(daemon, jid)["held_reason"] == "live"
    assert plat.signals == []
    # the explicit user action still works
    client(daemon, "cancel", job=a, terminate=True)
    assert any(s[1] == SIGTERM for s in plat.signals)


def test_workload_that_left_its_group_is_still_stoppable(daemon, clock, plat):
    """A workload that calls setsid() itself is no longer in the shim's group: killpg refuses (pgid mismatch), so the
    workload is signaled by pid (identity-checked) instead of never."""
    jid, w = submit_run(daemon, clock, checkpoint="restartable")
    w.rec.pgid = w.pid
    run_ticks(daemon, clock, 1)
    client(daemon, "cancel", job=jid)
    run_ticks(daemon, clock, 2)
    assert (w.pid, SIGTERM, jid) in plat.signals
    assert row(daemon, jid)["state"] == "cancelled"


def test_reconcile_maybe_released_row_without_record_is_lost_not_rerun(make_daemon, clock, plat, monkeypatch):
    """Review regression: the old daemon wrote the gate byte and died before mark_gate_released committed. The row says
    gate_released=0 with a pid. If the shim then dies without exit.json, the command may have run: never requeue it."""
    d = make_daemon()
    project(d, "p")
    monkeypatch.setattr(d.store, "mark_gate_released", lambda *a, **k: True)     # "crash" right after the gate write
    jid = client(d, "submit", spec=spec())["job"]
    run_ticks(d, clock, 2)
    r = row(d, jid)
    assert r["pid"] is not None and r["gate_released"] == 0
    d.stop()
    plat.kill_shim(jid)
    plat.workload(jid).exit(0, write_record=False)
    d2 = make_daemon()
    r = row(d2, jid)
    assert r["state"] == "lost" and r["attempt"] == 1
    assert jid not in plat.spawned[plat.spawned.index(jid) + 1:]                    # never spawned a second time
