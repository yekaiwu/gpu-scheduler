import dataclasses
import json
import os
import signal

import pytest

from gsched.platform.fake import SIGKILL, SIGTERM, SIGUSR1
from tests.daemon.conftest import ClientError, client, project, run_ticks, spec
from tests.policy.builders import ms


def at(clock, iso):
    clock.set_wall(ms(iso))


def row(d, jid):
    return d.store.get(jid)


def sub(d, **over):
    return client(d, "submit", spec=spec(**over))["job"]


def sigs(plat, *sigset):
    return [s for s in plat.signals if s[1] in sigset]


def test_dependency_waits_and_cascades(daemon, clock, plat):
    a = sub(daemon)
    b = client(daemon, "submit", spec=spec(after=[a]))["job"]
    c = client(daemon, "submit", spec=spec(after=[b]))["job"]
    run_ticks(daemon, clock, 3)
    assert row(daemon, a)["state"] == "held" and row(daemon, b)["state"] == "queued"
    v = client(daemon, "get", job=b)
    assert v["blocked_by"] == ["dependency"] and a in v["why"]
    plat.workload(a).exit(1)
    run_ticks(daemon, clock, 3)
    assert row(daemon, b)["state"] == "cancelled" and row(daemon, b)["end_reason"] == "dependency_failed"
    assert row(daemon, c)["state"] == "cancelled" and row(daemon, c)["end_reason"] == "dependency_failed"


def test_dependency_runs_after_success(daemon, clock, plat):
    a = sub(daemon)
    b = client(daemon, "submit", spec=spec(after=[a]))["job"]
    run_ticks(daemon, clock, 3)
    plat.workload(a).exit(0)
    run_ticks(daemon, clock, 3)
    assert row(daemon, b)["state"] == "held"


def test_one_admit_per_tick_then_hint_admits_next(daemon, clock):
    from tests.helpers import make_spec
    ids = [daemon.store.insert_job(make_spec(cwd="/tmp"), queue="day", state="queued", door="cli",
                                   now_ms=clock.now_ms()) for _ in range(3)]
    daemon.tick(admission_only=True)
    assert len([i for i in ids if row(daemon, i)["state"] == "held"]) == 1      # one admit per tick call
    assert daemon.admit_hint                                                      # ... and the loop is told to go again
    daemon.step(0)
    assert len([i for i in ids if row(daemon, i)["state"] == "held"]) == 2
    daemon.step(0)
    assert len([i for i in ids if row(daemon, i)["state"] == "held"]) == 3


# ---- cooperative / morning ---------------------------------------------------------------------------------
def coop_night(d, clock, plat, **over):
    at(clock, "2026-11-08T23:30:00Z")
    daemon_tick = lambda n: run_ticks(d, clock, n)
    jid = sub(d, checkpoint="cooperative", runtime={"estimate_s": 20000}, **over)
    daemon_tick(3)
    assert row(d, jid)["queue"] == "night" and row(d, jid)["held_reason"] == "live"
    return jid


def test_cooperative_checkpoint_at_0800_not_killed_on_ignore(daemon, clock, plat):
    jid = coop_night(daemon, clock, plat)
    w = plat.workload(jid)
    w.on_signal(SIGUSR1, "ignore")
    at(clock, "2026-11-09T07:59:00Z")
    run_ticks(daemon, clock, 2)
    assert plat.signals == []
    at(clock, "2026-11-09T08:00:01Z")
    run_ticks(daemon, clock, 2)
    assert [s[1] for s in plat.signals] == [SIGUSR1]                 # exactly one USR1, to the workload pid
    assert plat.signals[0][0] == w.pid
    assert row(daemon, jid)["held_reason"] == "checkpointing"
    run_ticks(daemon, clock, 110)
    assert row(daemon, jid)["held_reason"] == "checkpointing"
    run_ticks(daemon, clock, 12)
    r = row(daemon, jid)
    assert r["held_reason"] == "spilled" and r["spilled"] == 1 and r["spill_reason"] == "checkpoint_timeout"
    run_ticks(daemon, clock, 600)
    assert [s[1] for s in plat.signals] == [SIGUSR1]                 # nothing further, ever
    v = client(daemon, "get", job=jid)
    assert v["flags"]["spilled"] and v["flags"]["blocking_day"]
    assert "spill" in [e["kind"] for e in daemon.store.events(job=jid, since_ms=0)]
    assert [e for e in daemon.store.events(since_ms=0) if e["kind"] == "morning_edge"]
    # the morning edge happens once per civil date
    assert len([e for e in daemon.store.events(since_ms=0) if e["kind"] == "morning_edge"]) == 1


def test_cooperative_honours_checkpoint_and_resumes_same_id(daemon, clock, plat):
    jid = coop_night(daemon, clock, plat)
    w = plat.workload(jid)
    w.on_signal(SIGUSR1, "checkpoint_and_exit")
    at(clock, "2026-11-09T08:00:01Z")
    run_ticks(daemon, clock, 3)
    r = row(daemon, jid)
    assert r["state"] == "queued" and r["attempt"] == 2 and r["resume"] == 1 and r["queue"] == "night"
    assert [s[1] for s in plat.signals] == [SIGUSR1]
    # resumes tonight with GSCHED_RESUME=1
    at(clock, "2026-11-09T23:30:00Z")
    run_ticks(daemon, clock, 3)
    w2 = plat.workload(jid)
    assert w2.env["GSCHED_RESUME"] == "1" and w2.env["GSCHED_ATTEMPT"] == "2"
    assert row(daemon, jid)["held_reason"] == "live"


def test_exit_without_ready_is_done(daemon, clock, plat):
    jid = coop_night(daemon, clock, plat)
    w = plat.workload(jid)
    at(clock, "2026-11-09T08:00:01Z")
    run_ticks(daemon, clock, 2)
    w.exit(0)               # exits 0 but never wrote READY: a normal completion
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["state"] == "done"


def test_stale_ready_deleted_before_request(daemon, clock, plat):
    jid = coop_night(daemon, clock, plat)
    ck = row(daemon, jid)["checkpoint_dir"]
    os.makedirs(ck, exist_ok=True)
    with open(os.path.join(ck, "READY"), "w") as fh:
        fh.write("ready\n")
    at(clock, "2026-11-09T08:00:01Z")
    run_ticks(daemon, clock, 2)
    assert not os.path.exists(os.path.join(ck, "READY"))
    plat.workload(jid).exit(0)              # exit 0 without a *new* READY: done, not requeued
    run_ticks(daemon, clock, 2)
    assert row(daemon, jid)["state"] == "done"


def test_higher_priority_day_job_waits_during_atomic_spill(daemon, clock, plat):
    jid = coop_night(daemon, clock, plat, resources={"memory_gib": 30, "gpu": "exclusive"})
    at(clock, "2026-11-09T08:00:01Z")
    run_ticks(daemon, clock, 3)
    run_ticks(daemon, clock, 125)
    assert row(daemon, jid)["spilled"] == 1
    hi = sub(daemon, priority=50, resources={"memory_gib": 20, "gpu": "exclusive"})
    run_ticks(daemon, clock, 30)
    v = client(daemon, "get", job=hi)
    assert v["state"] == "queued" and "gpu" in v["blocked_by"]
    assert [s[1] for s in plat.signals] == [SIGUSR1]            # the spilled job is treated as atomic


def test_night_job_does_not_start_in_day_by_default(daemon, clock):
    jid = sub(daemon, queue="night", runtime={"estimate_s": 3600})
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "queued" and v["blocked_by"] == ["window"] and "23:00" in v["why"]
    at(clock, "2026-11-08T23:00:30Z")
    run_ticks(daemon, clock, 3)
    assert row(daemon, jid)["state"] == "held"


def test_atomic_24h_waits_for_day_via_rpc(daemon, clock):
    at(clock, "2026-11-08T23:00:30Z")
    long_ = sub(daemon, runtime={"estimate_s": 24 * 3600})
    short = sub(daemon, runtime={"estimate_s": 3600})
    run_ticks(daemon, clock, 3)
    assert row(daemon, long_)["state"] == "queued" and row(daemon, short)["state"] == "held"
    v = client(daemon, "get", job=long_)
    assert v["blocked_by"] == ["fit_window"] and "allow_spill" in v["why"]


# ---- the property test: atomic is never signaled by policy ----------------------------------------------------------
@pytest.mark.parametrize("path", ["morning", "preempt", "reservation", "pressure", "protective", "limit", "ceiling",
                                  "footprint", "drain", "cancel", "idle"])
def test_atomic_never_signaled(make_daemon, clock, plat, path):
    from gsched.config import Config
    cfgx = Config.defaults().replace(enforce=True, timezone="UTC", preempt_restartable=True)
    d = make_daemon(cfgx)
    project(d, "p")
    at(clock, "2026-11-08T23:30:00Z")
    over = {"resources": {"memory_gib": 10, "gpu": "exclusive"}, "runtime": {"estimate_s": 30000}}
    if path == "limit":
        over["runtime"] = {"estimate_s": 30000, "limit_s": 5}
    if path == "ceiling":
        over["resources"] = {"memory_gib": 10, "gpu": "exclusive", "ceiling_gib": 1}
    atomic = sub(d, checkpoint="atomic", **over)
    run_ticks(d, clock, 3)
    wa = plat.workload(atomic)
    wa.set_footprint(5)
    # a spilled cooperative job alongside
    coop = sub(d, checkpoint="cooperative", resources={"memory_gib": 3, "gpu": "none"}, runtime={"estimate_s": 30000})
    run_ticks(d, clock, 3)
    wc = plat.workload(coop)
    d.store.mark_cancel_requested  # noqa
    d.store.request_checkpoint(coop, clock.now_ms(), "morning")
    d.store.mark_spilled(coop, "checkpoint_timeout")
    protected_pids = {wa.pid, wc.pid}
    protected_pgids = {-wa.rec.pgid, -wc.rec.pgid}
    # maximally trigger the path
    if path == "morning":
        at(clock, "2026-11-09T08:00:30Z")
        sub(d, priority=9, resources={"memory_gib": 30, "gpu": "exclusive"})
    elif path in ("preempt", "reservation"):
        sub(d, priority=99, resources={"memory_gib": 30, "gpu": "exclusive"})
    elif path == "pressure":
        plat.set_mem(level=5, swap_used_gib=3)
    elif path == "protective":
        plat.add_foreign("llama-server", 0.0)
    elif path == "footprint":
        plat.set_footprint_unreadable(wa.pid)
        plat.set_footprint_unreadable(wc.pid)
    elif path == "drain":
        client(d, "drain")
    elif path == "cancel":
        client(d, "cancel", job=atomic)
        client(d, "cancel", job=coop)
    elif path == "idle":
        pass
    run_ticks(d, clock, 20)
    for pid, sig, jid in plat.signals:
        assert abs(pid) not in {abs(x) for x in protected_pids | protected_pgids}, (path, plat.signals)
    assert row(d, atomic)["state"] == "held" or path == "limit" and row(d, atomic)["state"] == "held"
    if path == "limit":
        assert row(d, atomic)["over_limit"] == 1
    if path == "footprint":
        assert row(d, atomic)["footprint_fail_n"] >= 5


# ---- protective mode ------------------------------------------------------------------------------------------------------
def test_foreign_heavy_any_footprint_protective_not_signaled(daemon, clock, plat):
    pid = plat.add_foreign("llama-server", 0.01, path="/opt/homebrew/bin/llama-server")
    run_ticks(daemon, clock, 2)
    st = client(daemon, "status")
    assert st["protective"] and st["offenders"][0]["class"] == "foreign_heavy"
    gpu = sub(daemon, resources={"memory_gib": 2, "gpu": "exclusive"})
    cpu = sub(daemon)
    run_ticks(daemon, clock, 3)
    assert row(daemon, gpu)["state"] == "queued" and row(daemon, cpu)["state"] == "held"   # CPU jobs still admit
    v = client(daemon, "get", job=gpu)
    assert v["blocked_by"] == ["protective"] and "llama-server" in v["why"]
    assert plat.signals == [] and plat.proc(pid) is not None


def test_protective_clears_after_30s(daemon, clock, plat):
    pid = plat.add_foreign("ollama", 0.0)
    run_ticks(daemon, clock, 2)
    assert daemon.protective.active
    plat.remove(pid)
    run_ticks(daemon, clock, 25)
    assert daemon.protective.active
    run_ticks(daemon, clock, 7)
    assert not daemon.protective.active
    kinds = [e["kind"] for e in daemon.store.events(since_ms=0)]
    assert "protective_enter" in kinds and "protective_exit" in kinds
    # a footprint drop alone does not clear it
    pid2 = plat.add_foreign("vllm", 10)
    run_ticks(daemon, clock, 2)
    plat.set_footprint(pid2, 0.0)
    run_ticks(daemon, clock, 60)
    assert daemon.protective.active


def test_protective_unloads_our_restartable_gpu_only(daemon, clock, plat):
    srv = sub(daemon, checkpoint="restartable", resources={"memory_gib": 4, "gpu": "exclusive"})
    cpu = sub(daemon, checkpoint="restartable", resources={"memory_gib": 1, "gpu": "none"})
    run_ticks(daemon, clock, 3)
    assert row(daemon, srv)["held_reason"] == "live"
    plat.add_foreign("llama-server", 1.0)
    run_ticks(daemon, clock, 4)
    assert row(daemon, srv)["state"] == "queued" and row(daemon, srv)["attempt"] == 2        # unloaded, requeued
    assert row(daemon, cpu)["held_reason"] == "live"
    assert all(s[0] < 0 and abs(s[0]) != 0 for s in plat.signals)
    foreign_pids = {p.pid for p in plat.processes() if p.comm.startswith("llama")}
    assert not any(s[0] in foreign_pids for s in plat.signals)


def test_foreign_port_listener_is_heavy(make_daemon, clock, plat):
    from gsched.config import Config
    d = make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", heavy_listen_ports=(8089,)))
    plat.add_foreign("mystery", 0.2, port=8089)
    run_ticks(d, clock, 2)
    assert d.protective.active
    st = client(d, "status")
    assert st["offenders"][0]["port"] == 8089


def test_offender_one_row_per_instance(daemon, clock, plat):
    pid = plat.add_foreign("llama-server", 3)
    run_ticks(daemon, clock, 10)
    rows = daemon.store.conn.execute("select * from offender where class='foreign_heavy'").fetchall()
    assert len(rows) == 1 and rows[0]["cleared_ms"] is None
    plat.remove(pid)
    run_ticks(daemon, clock, 2)
    rows = daemon.store.conn.execute("select * from offender where class='foreign_heavy'").fetchall()
    assert len(rows) == 1 and rows[0]["cleared_ms"] is not None


def test_browser_footprint_is_record_only_daemon(daemon, clock, plat):
    plat.add_foreign("Browser", 8, path="/Applications/Browser.app/Contents/MacOS/Browser")
    run_ticks(daemon, clock, 3)
    st = client(daemon, "status")
    assert not st["protective"] and st["offenders"][0]["class"] == "noted" and plat.signals == []
    # the live memory gate still accounts for it
    j = sub(daemon, resources={"memory_gib": 36, "gpu": "none"})
    run_ticks(daemon, clock, 3)
    assert client(daemon, "get", job=j)["blocked_by"] == ["memory_live"]


# ---- pressure --------------------------------------------------------------------------------------------------------------
def test_pressure_warn_freezes_admission_daemon(daemon, clock, plat):
    plat.set_mem(level=25)
    run_ticks(daemon, clock, 2)
    j = sub(daemon)
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=j)
    assert v["state"] == "queued" and "pressure" in v["blocked_by"] and "paused" in v["why"]
    plat.set_mem(level=80)
    run_ticks(daemon, clock, 3)
    assert row(daemon, j)["state"] == "held"


def test_swap_unknown_fails_closed_daemon(daemon, clock, plat):
    plat.set_mem(swap_unreadable=True)
    run_ticks(daemon, clock, 2)
    j = sub(daemon)
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=j)
    assert v["state"] == "queued" and "swap_unknown" in v["blocked_by"]


def test_pressure_critical_unloads_lowest_restartable_one_per_step(daemon, clock, plat):
    ids = [sub(daemon, checkpoint="restartable", priority=p, resources={"memory_gib": 2, "gpu": "none"})
           for p in (5, -5, 0)]
    atom = sub(daemon, checkpoint="atomic", priority=-100, resources={"memory_gib": 2, "gpu": "none"})
    run_ticks(daemon, clock, 5)
    plat.set_mem(level=5)
    run_ticks(daemon, clock, 2)
    stopped = [i for i in ids if row(daemon, i)["state"] == "queued" or row(daemon, i)["held_reason"] == "stopping"]
    assert stopped == [ids[1]]                                     # lowest priority first, only one
    run_ticks(daemon, clock, 5)
    assert len([s for s in plat.signals if s[1] == SIGTERM]) == 1
    run_ticks(daemon, clock, 6)
    assert len({s[0] for s in plat.signals if s[1] == SIGTERM}) == 2     # the next step, a second victim
    assert row(daemon, atom)["held_reason"] == "live" and not [s for s in plat.signals if s[2] == atom]


# ---- preemption flows -----------------------------------------------------------------------------------------------------------
def test_preempt_restartable_flow_and_target_gets_space(make_daemon, clock, plat):
    from gsched.config import Config
    d = make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", preempt_restartable=True))
    project(d, "p")
    srv = sub(d, checkpoint="restartable", priority=0, resources={"memory_gib": 30, "gpu": "exclusive"})
    run_ticks(d, clock, 70)
    hi = sub(d, priority=5, resources={"memory_gib": 20, "gpu": "exclusive"})
    low = sub(d, priority=0, resources={"memory_gib": 5, "gpu": "none"})
    run_ticks(d, clock, 2)
    assert row(d, srv)["held_reason"] in ("stopping",) or row(d, srv)["state"] == "queued"
    run_ticks(d, clock, 4)
    assert row(d, hi)["state"] == "held"                       # the target got the freed space first
    assert row(d, srv)["state"] == "queued"                    # unloaded, waiting for its turn
    assert [e["kind"] for e in d.store.events(since_ms=0) if e["kind"] == "preempt_plan"]


def test_restartable_not_preempted_before_min_hold(make_daemon, clock, plat):
    from gsched.config import Config
    d = make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", preempt_restartable=True))
    project(d, "p")
    srv = sub(d, checkpoint="restartable", resources={"memory_gib": 30, "gpu": "exclusive"})
    run_ticks(d, clock, 3)
    sub(d, priority=5, resources={"memory_gib": 20, "gpu": "exclusive"})
    run_ticks(d, clock, 20)
    assert row(d, srv)["held_reason"] == "live" and plat.signals == []


def test_reservation_after_starve_blocks_small_jobs(daemon, clock, plat):
    big_runner = sub(daemon, resources={"memory_gib": 30, "gpu": "none"})
    run_ticks(daemon, clock, 3)
    big = sub(daemon, resources={"memory_gib": 20, "gpu": "none"}, priority=1)
    run_ticks(daemon, clock, 3)
    assert row(daemon, big)["state"] == "queued" and row(daemon, big)["eligible_since_ms"]
    small = sub(daemon, resources={"memory_gib": 2, "gpu": "none"})
    run_ticks(daemon, clock, 3)
    assert row(daemon, small)["state"] == "held"                     # backfilled before starvation
    plat.workload(small).exit(0)
    clock.advance(4 * 3600 + 5)
    run_ticks(daemon, clock, 2)
    small2 = sub(daemon, resources={"memory_gib": 2, "gpu": "none"})
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=small2)
    assert v["state"] == "queued" and v["blocked_by"] == ["reservation"]
    assert client(daemon, "status")["reservation"] == big
    plat.workload(big_runner).exit(0)
    run_ticks(daemon, clock, 3)
    assert row(daemon, big)["state"] == "held"


def test_limit_restartable_term(daemon, clock, plat):
    j = sub(daemon, checkpoint="restartable", runtime={"estimate_s": 60, "limit_s": 30})
    run_ticks(daemon, clock, 20)
    assert plat.signals == []
    run_ticks(daemon, clock, 15)
    assert any(s[1] == SIGTERM for s in plat.signals)
    run_ticks(daemon, clock, 3)
    r = row(daemon, j)
    assert r["state"] == "failed" and r["end_reason"] == "limit"


def test_limit_cooperative_sends_one_usr1(daemon, clock, plat):
    j = sub(daemon, checkpoint="cooperative", runtime={"estimate_s": 60, "limit_s": 30})
    run_ticks(daemon, clock, 2)
    w = plat.workload(j)
    w.on_signal(SIGUSR1, "checkpoint_and_exit")
    run_ticks(daemon, clock, 35)
    assert [s[1] for s in plat.signals] == [SIGUSR1]
    assert row(daemon, j)["attempt"] == 2


def test_limit_atomic_over_limit_flag_only(daemon, clock, plat):
    j = sub(daemon, runtime={"estimate_s": 60, "limit_s": 10})
    run_ticks(daemon, clock, 20)
    assert row(daemon, j)["over_limit"] == 1 and plat.signals == []
    assert any(w["kind"] == "over_limit" for w in client(daemon, "status")["warnings"])


def test_estimate_overrun_warning(daemon, clock, plat):
    j = sub(daemon, runtime={"estimate_s": 5})
    run_ticks(daemon, clock, 10)
    v = client(daemon, "get", job=j)
    assert v["flags"]["estimate_overrun"]
    assert any(w["kind"] == "estimate_overrun" for w in client(daemon, "status")["warnings"])


def test_kill_escalation_after_kill_grace_daemon(daemon, clock, plat):
    j = sub(daemon, checkpoint="restartable", runtime={"estimate_s": 60, "limit_s": 5})
    run_ticks(daemon, clock, 2)
    plat.workload(j).on_signal(SIGTERM, "ignore")
    run_ticks(daemon, clock, 12)
    assert len(sigs(plat, SIGTERM)) == 1 and not sigs(plat, SIGKILL)
    run_ticks(daemon, clock, 20)
    assert len(sigs(plat, SIGKILL)) == 1
    assert row(daemon, j)["state"] == "failed"


def test_ceiling_restartable_unloaded(daemon, clock, plat):
    j = sub(daemon, checkpoint="restartable", resources={"memory_gib": 4, "gpu": "none", "ceiling_gib": 3})
    run_ticks(daemon, clock, 2)
    plat.workload(j).set_footprint(3.5)
    run_ticks(daemon, clock, 4)
    assert any(s[1] == SIGTERM for s in plat.signals)
    assert [e for e in daemon.store.events(job=j, since_ms=0) if e["kind"] == "ceiling_exceeded"]


def test_footprint_unreadable_5x_daemon(daemon, clock, plat):
    j = sub(daemon, checkpoint="restartable")
    run_ticks(daemon, clock, 2)
    plat.set_footprint_unreadable(plat.workload(j).pid)
    run_ticks(daemon, clock, 4)
    assert plat.signals == []
    run_ticks(daemon, clock, 4)
    assert any(s[1] == SIGTERM for s in plat.signals)


def test_observe_mode_signals_nothing(make_daemon, clock, plat):
    from gsched.config import Config
    d = make_daemon(Config.defaults().replace(enforce=False, timezone="UTC"))
    plat.add_foreign("llama-server", 10)
    plat.set_mem(level=3)
    at(clock, "2026-11-09T08:00:30Z")
    run_ticks(d, clock, 10)
    assert plat.signals == [] and plat.spawned == []
    assert d.protective.active                       # it observes and records


def test_admission_only_tick_counts_unmeasured_job_as_pending(daemon, clock, plat):
    """Review regression: a job admitted after the last scan has no footprint yet. It must count as fully pending, or
    the admission-only tick that follows every submit admits the next job against a sample that predates the first."""
    plat.set_mem(reclaimable_gib=22)             # live = 22 - 2 headroom = 20 GiB; ledger budget 40 GiB
    run_ticks(daemon, clock, 2)
    a = sub(daemon, resources={"memory_gib": 15})
    b = sub(daemon, resources={"memory_gib": 15})
    for _ in range(5):
        daemon.step(0)                           # admission-only ticks only (no clock advance)
    assert row(daemon, a)["state"] == "held"
    assert row(daemon, b)["state"] == "queued"
    assert "memory_live" in client(daemon, "get", job=b)["blocked_by"]
    run_ticks(daemon, clock, 3)                  # full ticks: A measured at 0 GiB touched, still 15 GiB pending
    assert row(daemon, b)["state"] == "queued"
    plat.workload(a).set_footprint(15)           # A allocated: the sample reflects it, pending is gone
    plat.set_mem(reclaimable_gib=37)
    run_ticks(daemon, clock, 2)
    assert row(daemon, b)["state"] == "held"


def test_process_scan_failure_fails_closed(daemon, clock, plat, monkeypatch):
    """Review regression (ground rule 9): an unreadable process table is not an empty one. It must not clear protective
    mode, count as five unreadable footprints, or let anything be admitted (also not by admission-only ticks)."""
    from gsched.errors import PlatformError
    foreign = plat.add_foreign("llama-server", 10)
    r = sub(daemon, checkpoint="restartable", resources={"gpu": "none"})
    run_ticks(daemon, clock, 2)
    assert daemon.protective.active and row(daemon, r)["held_reason"] == "live"

    def broken():
        raise PlatformError("proc_listallpids failed")
    monkeypatch.setattr(plat, "processes", broken)
    run_ticks(daemon, clock, 1)                              # the daemon sees the failure
    q = sub(daemon, resources={"gpu": "none"})
    for _ in range(3):
        daemon.step(0)                                       # admission-only ticks right after the submit
    run_ticks(daemon, clock, 40)                             # > protective_clear_s, > 5 footprint reads
    assert row(daemon, q)["state"] == "queued"
    assert daemon.protective.active
    assert row(daemon, r)["held_reason"] == "live" and row(daemon, r)["footprint_fail_n"] == 0
    assert plat.signals == []
    assert daemon.store.events(kinds=["scan_failed"])
    monkeypatch.undo()
    run_ticks(daemon, clock, 2)
    assert row(daemon, q)["state"] == "held"
    plat.remove(foreign)
