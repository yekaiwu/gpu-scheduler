import pytest

from gsched.policy import admit, maintenance, morning, preempt, pressure
from gsched.policy.model import AbortLines, Baseline
from gsched.units import GiB, gib_to_bytes
from tests.policy.builders import SnapBuilder, cfg, ms

C = cfg()


# ---- morning edge ---------------------------------------------------------------------------------------
def test_morning_edge_once_per_civil_day():
    b = SnapBuilder(now="2026-11-08T08:00:00Z")
    p = morning.plan(b.build(C), C)
    assert p.set_meta == {"morning_latch": "2026-11-08"}
    b.morning_latch = "2026-11-08"
    assert morning.plan(b.build(C), C).is_empty()
    b2 = SnapBuilder(now="2026-11-09T07:59:59Z", morning_latch="2026-11-08")
    assert morning.plan(b2.build(C), C).is_empty()                 # still night
    b3 = SnapBuilder(now="2026-11-09T08:00:01Z", morning_latch="2026-11-08")
    assert morning.plan(b3.build(C), C).set_meta == {"morning_latch": "2026-11-09"}


def test_daemon_started_at_1000_takes_edge_once():
    b = SnapBuilder(now="2026-11-08T10:00:00Z")
    p = morning.plan(b.build(C), C)
    assert p.set_meta["morning_latch"] == "2026-11-08"
    b.morning_latch = p.set_meta["morning_latch"]
    assert morning.plan(b.build(C), C).is_empty()


def test_morning_never_runs_with_window_disabled_or_observe():
    assert morning.plan(SnapBuilder().build(cfg(night_window=None)), cfg(night_window=None)).is_empty()
    assert morning.plan(SnapBuilder(now="2026-11-08T09:00:00Z").build(cfg(enforce=False)), cfg(enforce=False)).is_empty()


def test_morning_checkpoints_night_cooperative_not_day():
    b = SnapBuilder(now="2026-11-08T08:00:00Z")
    b.running("nc", checkpoint="cooperative", queue="night")
    b.running("dc", checkpoint="cooperative", queue="day")
    b.running("spilled", checkpoint="cooperative", queue="night", spilled=True, held_reason="spilled")
    p = morning.plan(b.build(C), C)
    assert [c[0] for c in p.checkpoint] == ["nc"] and p.checkpoint[0][1] == "morning"


def test_morning_unloads_restartable_even_if_preempt_off():
    b = SnapBuilder(now="2026-11-08T08:00:00Z", reclaimable_gib=40)
    b.running("srv", checkpoint="restartable", gpu="exclusive", mem=30, started_ago=3600, priority=50, spawn="daemon")
    b.queued("job", gpu="exclusive", mem=20, queue="day", priority=0)
    cfgx = cfg(preempt_restartable=False)
    p = morning.plan(b.build(cfgx), cfgx)
    assert p.stop == [("srv", "morning")] and p.reserve == "job" and p.freeze_admission
    c2 = cfg(morning_unload_restartable=False)
    assert morning.plan(b.build(c2), c2).stop == []


def test_morning_respects_min_hold_and_hold_flag():
    b = SnapBuilder(now="2026-11-08T08:00:00Z", reclaimable_gib=40)
    b.running("srv", checkpoint="restartable", gpu="exclusive", mem=30, started_ago=30)
    b.queued("job", gpu="exclusive", mem=20)
    assert morning.plan(b.build(C), C).stop == []
    b = SnapBuilder(now="2026-11-08T08:00:00Z", reclaimable_gib=40)
    b.running("srv", checkpoint="restartable", gpu="exclusive", mem=30, started_ago=3000, hold=True)
    b.queued("job", gpu="exclusive", mem=20)
    assert morning.plan(b.build(C), C).stop == []


def test_morning_never_signals_atomic():
    b = SnapBuilder(now="2026-11-08T08:00:00Z", reclaimable_gib=40)
    b.running("at", checkpoint="atomic", gpu="exclusive", mem=30, queue="night")
    b.running("sp", checkpoint="cooperative", mem=5, spilled=True, held_reason="spilled", queue="night")
    b.queued("job", gpu="exclusive", mem=20)
    p = morning.plan(b.build(C), C)
    assert p.stop == [] and p.checkpoint == []


# ---- preemption --------------------------------------------------------------------------------------------
def pre(b, c=C):
    return preempt.plan(b.build(c), c)


def test_preempt_restartable_flag_off_no_fairness_preempt():
    b = SnapBuilder(reclaimable_gib=40).running("srv", checkpoint="restartable", gpu="exclusive", mem=30,
                                                 priority=0, started_ago=3600)
    b.queued("hi", gpu="exclusive", mem=10, priority=5)
    assert pre(b).is_empty()
    p = pre(b, cfg(preempt_restartable=True))
    assert p.stop == [("srv", "preempt")] and p.reserve == "hi" and p.freeze_admission


def test_preempt_restartable_equal_priority_only_for_reservation():
    c = cfg(preempt_restartable=True)
    b = SnapBuilder(reclaimable_gib=40).running("srv", checkpoint="restartable", gpu="exclusive", mem=30,
                                                 priority=0, started_ago=3600)
    b.queued("same", gpu="exclusive", mem=10, priority=0)
    assert pre(b, c).is_empty()                                        # priority mode needs strictly lower
    b = SnapBuilder(reclaimable_gib=40).running("srv", checkpoint="restartable", gpu="exclusive", mem=30,
                                                 priority=0, started_ago=3600)
    b.queued("same", gpu="exclusive", mem=10, priority=0, eligible_since_ms=b.now_ms - 5 * 3600 * 1000)
    assert pre(b, c).stop == [("srv", "preempt")]                      # the reservation may displace equal priority


def test_equal_priority_does_not_checkpoint_cooperative():
    b = SnapBuilder(reclaimable_gib=40).running("co", checkpoint="cooperative", gpu="exclusive", mem=30,
                                                 priority=0, started_ago=7200)
    b.queued("same", gpu="exclusive", mem=10, priority=0)
    assert pre(b).is_empty()


def test_higher_priority_checkpoints_cooperative_after_coop_min_hold():
    def mk(ago):
        b = SnapBuilder(reclaimable_gib=40).running("co", checkpoint="cooperative", gpu="exclusive", mem=30,
                                                     priority=0, started_ago=ago)
        b.queued("hi", gpu="exclusive", mem=10, priority=5)
        return b
    assert pre(mk(299)).is_empty()
    p = pre(mk(300))
    assert p.checkpoint == [("co", "preempt")] and p.stop == [] and p.freeze_admission


def test_no_pointless_preemption():
    b = SnapBuilder(reclaimable_gib=40, memsize_gib=48)
    b.running("big", checkpoint="atomic", gpu="exclusive", mem=30)
    b.running("small", checkpoint="restartable", gpu="none", mem=2, started_ago=600)
    b.queued("hi", gpu="exclusive", mem=8, priority=9)
    assert pre(b, cfg(preempt_restartable=True)).is_empty()            # killing 'small' cannot free the GPU


def test_hold_resident_not_victim_except_pressure():
    c = cfg(preempt_restartable=True)
    b = SnapBuilder(reclaimable_gib=40).running("srv", checkpoint="restartable", gpu="exclusive", mem=30, hold=True,
                                                 started_ago=3600)
    b.queued("hi", gpu="exclusive", mem=10, priority=9)
    assert pre(b, c).is_empty()
    snap = b.build(cfg(preempt_restartable=True))
    assert preempt.find_victims(snap.jobs[1], snap, c, "pressure") == ["srv"]


def test_atomic_spilled_external_never_victims():
    c = cfg(preempt_restartable=True)
    b = SnapBuilder(reclaimable_gib=40)
    b.running("a", checkpoint="atomic", mem=10, started_ago=9999)
    b.running("s", checkpoint="cooperative", mem=10, started_ago=9999, spilled=True, held_reason="spilled")
    b.running("e", checkpoint="restartable", mem=10, started_ago=9999, spawn="external")
    b.running("c", checkpoint="cooperative", mem=10, started_ago=9999, held_reason="checkpointing")
    b.queued("hi", gpu="exclusive", mem=30, priority=9)
    snap = b.build(c)
    for mode in ("priority", "reservation", "morning", "pressure"):
        assert preempt.find_victims(snap.jobs[-1], snap, c, mode) == []


def test_victims_minimal_set_and_ordering():
    c = cfg(preempt_restartable=True)
    b = SnapBuilder(reclaimable_gib=40)
    b.running("r1", checkpoint="restartable", gpu="none", mem=15, priority=-5, started_ago=999)
    b.running("r2", checkpoint="restartable", gpu="none", mem=15, priority=-1, started_ago=999)
    b.queued("hi", gpu="none", mem=20, priority=3)
    snap = b.build(c)
    # budget 40, reserved 30: need 20 -> free at least 10; one victim is enough, lowest priority first
    assert preempt.find_victims(snap.jobs[-1], snap, c, "priority") == ["r1"]


def test_preempted_space_goes_to_target():
    c = cfg(preempt_restartable=True)
    b = SnapBuilder(reclaimable_gib=40).running("srv", checkpoint="restartable", gpu="exclusive", mem=30,
                                                 started_ago=3600, held_reason="stopping", stop_reason="preempt",
                                                 stop_requested_ms=None)
    b.queued("hi", gpu="none", mem=20, priority=5).queued("low", gpu="none", mem=10, priority=0)
    p = pre(b, c)
    assert p.freeze_admission and not p.stop                           # in flight: freeze admission, no new victims
    # victim gone: the target is still top ranked and gets the space first
    b2 = SnapBuilder(reclaimable_gib=40).queued("hi", gpu="none", mem=20, priority=5).queued("low", gpu="none", mem=10)
    assert admit.plan(b2.build(c), c).admit == "hi"


# ---- pressure ------------------------------------------------------------------------------------------------
def test_pressure_level_thresholds():
    b = SnapBuilder(level=80).build(C)
    assert pressure.level(b.mem, None, C)[0] == "normal"
    assert pressure.level(SnapBuilder(level=25).build(C).mem, None, C)[0] == "warn"
    assert pressure.level(SnapBuilder(level=10).build(C).mem, None, C)[0] == "critical"
    assert pressure.level(SnapBuilder(reclaimable_gib=3).build(C).mem, None, C)[0] == "warn"
    assert pressure.level(SnapBuilder(reclaimable_gib=1).build(C).mem, None, C)[0] == "critical"
    assert pressure.level(b.mem, 2.0, C)[0] == "warn"
    assert pressure.level(b.mem, 5.0, C)[0] == "critical"
    c2 = cfg(pressure={"warn": {"level_below": None, "swap_rise_gib_per_min": None, "reclaimable_below_gib": None},
                       "critical": {"level_below": None, "swap_rise_gib_per_min": None, "reclaimable_below_gib": None}})
    assert pressure.level(SnapBuilder(level=1).build(c2).mem, 99.0, c2)[0] == "normal"


def test_swap_unknown_fails_closed():
    snap = SnapBuilder(swap_known=False).build(C)
    lvl, why = pressure.level(snap.mem, None, C)
    assert lvl == "warn" and "swap_unknown" in why


def test_swap_window_rate():
    w = pressure.SwapWindow()
    assert w.rate_gib_per_min() is None
    w.add(0, 0)
    w.add(30_000, 2 * GiB)
    assert w.rate_gib_per_min() == pytest.approx(4.0)
    w.add(200_000, 2 * GiB)                    # old samples fall out of the 60 s window
    assert len(w.buf) == 1


def test_pressure_warn_freezes_admission():
    b = SnapBuilder(pressure="warn").queued("x")
    p = pressure.plan(b.build(C), C)
    assert p.freeze_admission and not p.stop


def test_pressure_critical_unloads_lowest_restartable_one_per_step():
    b = SnapBuilder(pressure="critical")
    b.running("r_hi", checkpoint="restartable", gpu="exclusive", priority=10, mem=5)
    b.running("r_lo", checkpoint="restartable", gpu="none", priority=-10, mem=5)
    b.running("atom", checkpoint="atomic", gpu="none", priority=-100, mem=5)
    p = pressure.plan(b.build(C), C)
    assert p.stop == [("r_lo", "pressure")] and p.set_meta["last_pressure_action_ms"] == str(b.now_ms)
    b.last_pressure_action_ms = b.now_ms - 5000
    assert pressure.plan(b.build(C), C).stop == []                       # inside pressure_step_s
    b.last_pressure_action_ms = b.now_ms - 10_000
    assert pressure.plan(b.build(C), C).stop == [("r_lo", "pressure")]


def test_pressure_never_signals_atomic():
    b = SnapBuilder(pressure="critical").running("atom", checkpoint="atomic", mem=5)
    b.running("sp", checkpoint="cooperative", mem=5, spilled=True, held_reason="spilled")
    p = pressure.plan(b.build(cfg(pressure_checkpoint_cooperative=True)), cfg(pressure_checkpoint_cooperative=True))
    assert not p.stop and not p.checkpoint and p.alarm == ["pressure_critical_no_action"]
    b.last_alarm_ms = b.now_ms - 60_000
    assert pressure.plan(b.build(C), C).alarm == []                      # rate limited to once per 5 min


def test_pressure_cooperative_only_when_enabled():
    b = SnapBuilder(pressure="critical").running("co", checkpoint="cooperative", mem=5)
    assert pressure.plan(b.build(C), C).checkpoint == []
    c = cfg(pressure_checkpoint_cooperative=True)
    assert pressure.plan(b.build(c), c).checkpoint == [("co", "pressure")]


def test_protective_unloads_our_restartable_gpu_only():
    b = SnapBuilder(protective=True)
    b.running("srv", checkpoint="restartable", gpu="exclusive", mem=5)
    b.running("cpu", checkpoint="restartable", gpu="none", mem=5)
    b.running("atom", checkpoint="atomic", gpu="exclusive", mem=5)
    p = pressure.plan(b.build(C), C)
    assert p.stop == [("srv", "protective")]
    c = cfg(protective_unload_restartable=False)
    assert pressure.plan(b.build(c), c).stop == []


# ---- maintenance ------------------------------------------------------------------------------------------------------
def mnt(b, c=C):
    return maintenance.plan(b.build(c), c)


def test_limit_starts_at_process_start():
    # enqueued 10 hours ago, but the process started 3599 s ago: limit_s 3600 not reached
    def mk(started_ago):
        b = SnapBuilder()
        b.running("j", checkpoint="restartable", limit_s=3600, started_ago=started_ago, enqueued_ms=SnapBuilder().now_ms - 36000_000)
        return b
    assert mnt(mk(3599)).stop == []
    assert mnt(mk(3601)).stop == [("j", "limit")]


def test_limit_by_mode():
    b = SnapBuilder()
    b.running("co", checkpoint="cooperative", limit_s=60, started_ago=61)
    assert mnt(b).checkpoint == [("co", "limit")]
    b = SnapBuilder().running("ext", checkpoint="atomic", spawn="external", limit_s=60, started_ago=61)
    p = mnt(b)
    assert p.flags == [("ext", {"yield_requested": 1})] and not p.stop and not p.checkpoint


def test_limit_atomic_marks_over_limit_no_signal():
    b = SnapBuilder().running("at", checkpoint="atomic", limit_s=60, started_ago=3600)
    p = mnt(b)
    assert p.flags == [("at", {"over_limit": 1})] and not (p.stop or p.checkpoint or p.kill or p.kill_stop)
    b = SnapBuilder().running("at", checkpoint="atomic", limit_s=60, started_ago=3600, over_limit=True)
    assert mnt(b).is_empty() or not mnt(b).flags                         # flagged once
    b = SnapBuilder().running("sp", checkpoint="cooperative", limit_s=60, started_ago=3600, spilled=True,
                              held_reason="spilled")
    assert not (mnt(b).stop or mnt(b).checkpoint)


def test_kill_escalation_after_kill_grace():
    def mk(ago, **kw):
        b = SnapBuilder()
        b.running("j", checkpoint="restartable", held_reason="stopping", stop_reason="preempt", **kw)
        b.jobs[0] = __import__("dataclasses").replace(b.jobs[0], stop_requested_ms=b.now_ms - ago * 1000)
        return b
    assert mnt(mk(14)).kill == []
    assert mnt(mk(15)).kill == ["j"]
    b = mk(60)
    b.jobs[0] = __import__("dataclasses").replace(b.jobs[0], kill_sent=True)
    assert mnt(b).kill == []                                              # once


def test_checkpoint_grace_spills_or_kills():
    import dataclasses
    def mk(on_timeout, ago):
        b = SnapBuilder().running("co", checkpoint="cooperative", held_reason="checkpointing", grace_s=120,
                                  on_timeout=on_timeout)
        b.jobs[0] = dataclasses.replace(b.jobs[0], stop_requested_ms=b.now_ms - ago * 1000)
        return b
    assert mnt(mk("wait", 119)).is_empty()
    assert mnt(mk("wait", 121)).spill == [("co", "checkpoint_timeout")]
    p = mnt(mk("kill", 121))
    assert p.kill_stop == [("co", "checkpoint_timeout_kill")] and p.events[0][0] == "work_lost_since_last_ready"


def test_ceiling_unloads_restartable_only():
    b = SnapBuilder().running("r", checkpoint="restartable", mem=5, footprint_gib=9, ceiling_gib=8)
    p = mnt(b)
    assert p.stop == [("r", "ceiling")] and p.events[0][0] == "ceiling_exceeded"
    b = SnapBuilder().running("a", checkpoint="atomic", mem=5, footprint_gib=9, ceiling_gib=8)
    p = mnt(b)
    assert not p.stop and p.events[0][0] == "ceiling_exceeded"
    b = SnapBuilder().running("c", checkpoint="cooperative", mem=5, footprint_gib=9, ceiling_gib=8)
    assert not mnt(b).stop


def test_footprint_unreadable_5x():
    b = SnapBuilder().running("r", checkpoint="restartable", footprint_fail_n=5, footprint_gib=None)
    assert mnt(b).stop == [("r", "footprint_unreadable")]
    b = SnapBuilder().running("r", checkpoint="restartable", footprint_fail_n=4, footprint_gib=None)
    assert mnt(b).stop == []
    b = SnapBuilder().running("a", checkpoint="atomic", footprint_fail_n=5, footprint_gib=None)
    p = mnt(b)
    assert not p.stop and p.events and p.events[0][0] == "footprint_unreadable"


def test_estimate_overrun_flagged_once():
    b = SnapBuilder().running("a", estimate_s=100, started_ago=101)
    assert mnt(b).flags == [("a", {"estimate_overrun": 1})]
    b = SnapBuilder().running("a", estimate_s=100, started_ago=101, estimate_overrun=True)
    assert mnt(b).flags == []


def test_idle_unload_conditions():
    mk = lambda **kw: SnapBuilder().running("srv", checkpoint="restartable", profile="p1", idle_unload_s=1800, ready=True,
                                           last_touch_ms=SnapBuilder().now_ms - 1801_000, **kw)
    assert mnt(mk()).stop == [("srv", "idle")]
    assert mnt(mk(hold=True)).stop == []
    b = SnapBuilder().running("srv", checkpoint="restartable", profile="p1", idle_unload_s=1800, ready=True,
                              last_touch_ms=SnapBuilder().now_ms - 100_000)
    assert mnt(b).stop == []
    b = SnapBuilder().running("srv", checkpoint="restartable", profile="p1", idle_unload_s=1800, ready=False,
                              last_touch_ms=SnapBuilder().now_ms - 9999_000)
    assert mnt(b).stop == []                                              # not ready yet


def test_switch_after_min_hold():
    mk = lambda ago: SnapBuilder().running("a", checkpoint="restartable", profile="p1", switch_to="p2", started_ago=ago)
    assert mnt(mk(59)).stop == [] and mnt(mk(61)).stop == [("a", "switch")]


def test_drain_unloads_unheld_residents():
    b = SnapBuilder(draining=True).running("a", checkpoint="restartable", profile="p1").running(
        "b", checkpoint="restartable", profile="p2", hold=True)
    assert mnt(b).stop == [("a", "drain")]


def test_abort_lines_only_after_ready():
    ab = AbortLines(level_below=40)
    b = SnapBuilder(level=30).running("srv", checkpoint="restartable", profile="p1", abort=ab, ready=True)
    b.baselines["srv"] = Baseline(0, 80, 20 * GiB)
    p = mnt(b)
    assert p.stop == [("srv", "abort")] and p.events[0][0] == "abort_tripped"
    b = SnapBuilder(level=30).running("srv", checkpoint="restartable", profile="p1", abort=ab, ready=False)
    b.baselines["srv"] = Baseline(0, 80, 20 * GiB)
    assert mnt(b).stop == []
    ab2 = AbortLines(swap_rise_gib=1.0)
    b = SnapBuilder(swap_used_gib=3).running("srv", checkpoint="restartable", profile="p1", abort=ab2)
    b.baselines["srv"] = Baseline(gib_to_bytes(1), 80, 20 * GiB)
    assert mnt(b).stop == [("srv", "abort")]
    ab3 = AbortLines(ceiling_gib=4)
    b = SnapBuilder().running("srv", checkpoint="restartable", profile="p1", abort=ab3, footprint_gib=5, mem=5)
    b.baselines["srv"] = Baseline(0, 80, 20 * GiB)
    assert mnt(b).stop == [("srv", "abort")]
    ab4 = AbortLines(level_below=10)
    b = SnapBuilder().running("srv", checkpoint="restartable", profile="p1", abort=ab4, abort_unavail_since_ms=SnapBuilder().now_ms - 31_000)
    b.baselines["srv"] = Baseline(0, 80, 20 * GiB)
    assert mnt(b).stop == [("srv", "abort")]


def test_morning_edge_nonwrapping_window_waits_for_end():
    """Review regression: with a 01:00-06:00 window, 00:30 is outside the window but before today's end. The edge
    must not fire then (it used to, and the latch then swallowed the real 06:00 edge)."""
    c = cfg(night_window={"start": "01:00", "end": "06:00"})
    assert morning.plan(SnapBuilder(now="2026-11-09T00:30:00Z", morning_latch="2026-11-08").build(c), c).is_empty()
    assert morning.plan(SnapBuilder(now="2026-11-09T03:00:00Z", morning_latch="2026-11-08").build(c), c).is_empty()
    p = morning.plan(SnapBuilder(now="2026-11-09T06:00:00Z", morning_latch="2026-11-08").build(c), c)
    assert p.set_meta == {"morning_latch": "2026-11-09"}
    # an evening-only window: 00:10 the next day is not an edge either
    c2 = cfg(night_window={"start": "20:00", "end": "23:30"})
    assert morning.plan(SnapBuilder(now="2026-11-09T00:10:00Z", morning_latch="2026-11-08").build(c2), c2).is_empty()
    assert morning.plan(SnapBuilder(now="2026-11-09T23:30:00Z", morning_latch="2026-11-08").build(c2),
                        c2).set_meta == {"morning_latch": "2026-11-09"}


def test_spilled_job_gets_overrun_and_over_limit_flags_no_signal():
    b = SnapBuilder().running("S", checkpoint="cooperative", held_reason="spilled", spilled=True, checkpoint_failed=True,
                              started_ago=7200, estimate_s=3600, limit_s=5400)
    p = maintenance.plan(b.build(C), C)
    flags = {k for _, f in p.flags for k in f}
    assert flags == {"over_limit", "estimate_overrun"}
    assert not (p.stop or p.kill or p.kill_stop or p.checkpoint or p.spill)
