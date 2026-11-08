import dataclasses

import pytest

from gsched.policy import admit, windows
from gsched.policy.fit import fits, ledger
from gsched.policy.rank import charge, decay, rank_key, share
from gsched.status import explain_all
from gsched.units import GiB
from tests.policy.builders import SnapBuilder, cfg, ms

C = cfg()


def plan(b, c=C):
    return admit.plan(b.build(c), c)


def test_one_exclusive_gpu_job():
    b = SnapBuilder().running("A", gpu="exclusive", mem=10).queued("B", gpu="exclusive", mem=5)
    p = plan(b)
    assert p.admit is None and p.blocked["B"] == ["gpu"]
    b = SnapBuilder().running("A", gpu="shared", mem=10).queued("B", gpu="exclusive", mem=5)
    assert plan(b).blocked["B"] == ["gpu"]
    b = SnapBuilder().running("A", gpu="exclusive", mem=10).queued("B", gpu="shared", mem=5)
    assert plan(b).blocked["B"] == ["gpu"]


def test_two_none_gpu_jobs_corun_when_memory_fits():
    b = SnapBuilder().running("A", gpu="none", mem=10).queued("B", gpu="none", mem=10)
    assert plan(b).admit == "B"
    b = SnapBuilder().running("A", gpu="exclusive", mem=10).queued("B", gpu="none", mem=10)
    assert plan(b).admit == "B"


def test_shared_gpu_max():
    b = SnapBuilder().running("A", gpu="shared").running("B", gpu="shared").queued("C", gpu="shared")
    assert plan(b).blocked["C"] == ["gpu"]
    b = SnapBuilder().running("A", gpu="shared").queued("C", gpu="shared")
    assert plan(b).admit == "C"
    assert plan(SnapBuilder().running("A", gpu="shared").running("B", gpu="shared").queued("C", gpu="shared"),
                cfg(gpu_shared_max=3)).admit == "C"


def test_memory_ledger_blocks():
    b = SnapBuilder(reclaimable_gib=48).running("A", mem=30).queued("B", mem=15)
    assert plan(b).blocked["B"] == ["memory_ledger"]
    b = SnapBuilder(reclaimable_gib=48).running("A", mem=30).queued("B", mem=10)
    assert plan(b).admit == "B"


def test_ledger_uses_max_of_reservation_and_footprint():
    b = SnapBuilder(reclaimable_gib=48).running("A", mem=10, footprint_gib=35).queued("B", mem=10)
    assert "memory_ledger" in plan(b).blocked["B"]
    led = ledger(b.build())
    assert led.reserved_bytes == 35 * GiB and led.pending_bytes == 0


def test_memory_live_blocks_when_ide_uses_ram():
    # an IDE ate the RAM: 5 GiB reclaimable, ledger is empty
    b = SnapBuilder(reclaimable_gib=5).queued("B", mem=10)
    assert plan(b).blocked["B"] == ["memory_live"]
    assert plan(SnapBuilder(reclaimable_gib=13).queued("B", mem=10)).admit == "B"        # 13 - 2 headroom >= 10


def test_pending_prevents_double_admit_before_allocation():
    # A was admitted a tick ago for 20 GiB and has touched nothing yet; the sample still says 22 reclaimable
    b = SnapBuilder(reclaimable_gib=22).running("A", mem=20, footprint_gib=0).queued("B", mem=5)
    assert "memory_live" in plan(b).blocked["B"]
    # once A has allocated, the sample reflects it and pending is gone
    b = SnapBuilder(reclaimable_gib=12).running("A", mem=20, footprint_gib=20).queued("B", mem=5)
    assert plan(b).admit == "B"


def test_unreadable_footprint_counts_as_reservation_no_pending():
    b = SnapBuilder(reclaimable_gib=30).running("A", mem=10, footprint_gib=None).queued("B", mem=5)
    led = ledger(b.build())
    assert led.reserved_bytes == 10 * GiB and led.pending_bytes == 0


def test_one_admit_per_tick():
    b = SnapBuilder().queued("A", mem=2).queued("B", mem=2).queued("C", mem=2)
    p = plan(b)
    assert p.admit == "A"           # exactly one id, never a list


def test_atomic_24h_does_not_start_at_2300():
    b = SnapBuilder(now="2026-11-08T23:00:00Z")
    b.queued("long", checkpoint="atomic", queue="night", estimate_s=24 * 3600, waited=3600)
    b.queued("short", checkpoint="atomic", queue="night", estimate_s=3600, waited=60)
    p = plan(b)
    assert p.admit == "short"
    ok, why = admit.eligible(b.build(C).jobs[0], b.build(C), C)
    assert (ok, why) == (False, "fit_window")
    # allow_spill lifts it
    b2 = SnapBuilder(now="2026-11-08T23:00:00Z").queued("long", queue="night", estimate_s=24 * 3600, allow_spill=True)
    assert plan(b2).admit == "long"


def test_fit_window_inside_window_ends_by_this_morning():
    # 02:00: 6 h left in the window (to 08:00). A 5 h job fits, a 7 h job does not
    b = SnapBuilder(now="2026-11-08T02:00:00Z").queued("a", queue="night", estimate_s=5 * 3600) \
        .queued("b", queue="night", estimate_s=7 * 3600)
    snap = b.build(C)
    assert admit.eligible(snap.jobs[0], snap, C)[0] and admit.eligible(snap.jobs[1], snap, C)[1] == "fit_window"


def test_backfill_default_false():
    b = SnapBuilder(now="2026-11-08T16:00:00Z").queued("n", queue="night", estimate_s=3600)
    p = plan(b)
    assert p.admit is None
    assert admit.eligible(b.build(C).jobs[0], b.build(C), C) == (False, "window")


def test_backfill_true_runs_night_job_when_no_day_waiting():
    c = cfg(backfill=True)
    b = SnapBuilder(now="2026-11-08T16:00:00Z").queued("n", queue="night", estimate_s=3600)
    assert plan(b, c).admit == "n"
    # a waiting day job blocks the backfill
    b = SnapBuilder(now="2026-11-08T16:00:00Z", reclaimable_gib=40).queued("n", queue="night", estimate_s=3600) \
        .queued("d", queue="day", gpu="exclusive").running("g", gpu="exclusive", mem=2)
    snap = b.build(c)
    assert admit.eligible(snap.jobs[0], snap, c) == (False, "window")
    # backfilling a 12 h atomic job at 16:00 must still end before tomorrow 08:00 (16 h away)
    b = SnapBuilder(now="2026-11-08T16:00:00Z").queued("n", queue="night", estimate_s=12 * 3600)
    assert plan(b, c).admit == "n"
    b = SnapBuilder(now="2026-11-08T16:00:00Z").queued("n", queue="night", estimate_s=20 * 3600)
    assert plan(b, c).admit is None


def test_day_job_eligible_at_night():
    b = SnapBuilder(now="2026-11-08T23:30:00Z").queued("d", queue="day")
    assert plan(b).admit == "d"


@pytest.mark.parametrize("tzname,before,after", [
    # spring forward 2026-03-08 02:00 PST -> 03:00 PDT: 23:00 local is still 23:00 local
    ("America/Los_Angeles", "2026-03-09T05:59:00Z", "2026-03-09T06:00:00Z"),     # 22:59 PDT / 23:00 PDT (UTC-7)
    # fall back 2026-11-01: 23:00 PST is UTC-8
    ("America/Los_Angeles", "2026-11-02T06:59:00Z", "2026-11-02T07:00:00Z"),     # 22:59 PST / 23:00 PST
])
def test_night_window_uses_civil_time(tzname, before, after):
    c = cfg(timezone=tzname)
    if tzname == "America/Los_Angeles" and before.startswith("2026-03"):
        before, after = "2026-03-10T05:59:00Z", "2026-03-10T06:00:00Z"            # PDT (UTC-7) -> 23:00 = 06:00Z
    assert not windows.in_night(ms(before), c)
    assert windows.in_night(ms(after), c)


def test_night_window_dst_end_date_and_next_end():
    c = cfg(timezone="America/Los_Angeles")
    # on the fall-back night (2026-11-01) the window end is 08:00 PST = 16:00Z
    now = ms("2026-11-01T09:00:00Z")           # 02:00 PST... after the repeated hour
    assert windows.next_window_end_ms(now, c) == ms("2026-11-01T16:00:00Z")
    # spring-forward night: 08:00 PDT = 15:00Z
    now = ms("2026-03-08T09:00:00Z")           # 01:00 PST
    assert windows.next_window_end_ms(now, c) == ms("2026-03-08T15:00:00Z")
    assert windows.civil_date(ms("2026-11-08T06:30:00Z"), c) == "2026-11-07"


def test_night_window_null():
    c = cfg(night_window=None)
    b = SnapBuilder(now="2026-11-08T16:00:00Z").queued("n", queue="night", estimate_s=10**6)
    assert plan(b, c).admit == "n" and not windows.in_night(b.now_ms, c)


def test_same_priority_alternates():
    c = cfg()
    usage = {"A": 0.0, "B": 0.0}
    order = []
    for rnd in range(8):
        b = SnapBuilder(reclaimable_gib=40)
        for p_, u in usage.items():
            b.project(p_, u)
        # four jobs each, enqueued in an interleaved but A-first order
        for i in range(4):
            b.queued(f"A{i}", project="A", gpu="exclusive", mem=4, enqueued_ms=1000 + i * 2)
            b.queued(f"B{i}", project="B", gpu="exclusive", mem=4, enqueued_ms=1001 + i * 2)
        # drop jobs already run
        ran = [o for o in order]
        b.jobs = [j for j in b.jobs if j.id not in ran]
        pl = admit.plan(b.build(c), c)
        order.append(pl.admit)
        proj = pl.admit[0]
        for k in usage:                                   # a 60 s exclusive job: decay everyone, charge the runner
            usage[k] = decay(usage[k], 60, c.fair_halflife_s) + (60 * 1.0 if k == proj else 0)
    assert [o[0] for o in order] == list("ABABABAB")


def test_priority_beats_usage():
    b = SnapBuilder().project("heavy", 10000).project("light", 0)
    b.queued("hi", project="heavy", priority=10, enqueued_ms=5)
    b.queued("lo", project="light", priority=0, enqueued_ms=1)
    b.running("x", gpu="exclusive")
    # both are gpu none by default so only one admit per tick: priority wins
    assert plan(b).admit == "hi"


def test_higher_priority_does_not_bypass_memory():
    b = SnapBuilder(reclaimable_gib=14).queued("big", priority=10, mem=30, waited=10).queued("small", mem=2)
    p = plan(b)
    assert p.admit == "small" and "memory_live" in p.blocked["big"]


def test_small_job_backfills_past_big_blocked_job():
    b = SnapBuilder(reclaimable_gib=40).running("A", mem=30).queued("big", mem=20, priority=5).queued("small", mem=3)
    assert plan(b).admit == "small"


def test_reservation_after_starve_s_blocks_competitors():
    big_since = lambda ago: SnapBuilder(reclaimable_gib=40).running("A", mem=30, footprint_gib=30) \
        .queued("big", mem=20, priority=5, eligible_since_ms=None)
    b = SnapBuilder(reclaimable_gib=40).running("A", mem=30)
    b.queued("big", mem=20, priority=5, eligible_since_ms=b.now_ms - 3 * 3600 * 1000)
    b.queued("small", mem=3)
    assert plan(b).admit == "small"                                   # not starving yet (3h < 4h)
    b = SnapBuilder(reclaimable_gib=40).running("A", mem=30)
    b.queued("big", mem=20, priority=5, eligible_since_ms=b.now_ms - 5 * 3600 * 1000)
    b.queued("small", mem=3)
    p = plan(b)
    assert p.admit is None and p.reserve == "big" and p.blocked["small"] == ["reservation"]
    snap = b.build(C)
    e = explain_all(snap, C)
    assert e["small"]["blocked_by"] == ["reservation"] and "holds the reservation" in e["small"]["why"]
    # a gpu-none job does not compete with a reservation that waits for the GPU
    b = SnapBuilder(reclaimable_gib=40).running("A", gpu="exclusive", mem=3)
    b.queued("big", gpu="exclusive", mem=3, priority=5, eligible_since_ms=b.now_ms - 5 * 3600 * 1000)
    b.queued("cpu", gpu="none", mem=3)
    assert plan(b).admit == "cpu"


def test_reservation_not_created_for_pressure_or_protective():
    for kw in ({"pressure": "warn"}, {"protective": True}):
        b = SnapBuilder(**kw)
        b.queued("big", gpu="exclusive", mem=3, eligible_since_ms=b.now_ms - 9 * 3600 * 1000)
        b.queued("cpu", gpu="none", mem=3)
        p = plan(b)
        assert p.reserve is None
        if "protective" in kw:
            assert p.admit == "cpu"                                  # protective blocks only GPU jobs


def test_pressure_and_swap_unknown_block_everything():
    assert "pressure" in plan(SnapBuilder(pressure="warn").queued("a")).blocked["a"]
    assert "swap_unknown" in plan(SnapBuilder(swap_known=False).queued("a")).blocked["a"]
    assert "level" in plan(SnapBuilder(level=20).queued("a", level_min=50)).blocked["a"]
    assert plan(SnapBuilder(level=60).queued("a", level_min=50)).admit == "a"


def test_lock_conflict():
    b = SnapBuilder().running("A", locks=["ds"]).queued("B", locks=["ds"]).queued("C", locks=["other"])
    p = plan(b)
    assert p.blocked["B"] == ["lock:ds"] and p.admit == "C"


def test_cpu_budget_soft_limit():
    b = SnapBuilder(cpu=8).running("A", cpu_cores=6).queued("B", cpu_cores=4)
    assert plan(b).blocked["B"] == ["cpu"]


def test_external_not_eligible_without_start_waiter():
    b = SnapBuilder().queued("E", spawn="external")
    assert plan(b).admit is None
    assert admit.eligible(b.build(C).jobs[0], b.build(C), C) == (False, "no_start_waiter")
    b = SnapBuilder().queued("E", spawn="external", start_waiter_parked=True)
    assert plan(b).admit == "E"


@pytest.mark.parametrize("mem,reserve_expect", [(16, 8), (512, 64)])
def test_gate_uses_budget_not_memsize_constant(mem, reserve_expect):
    b = SnapBuilder(memsize_gib=mem, reclaimable_gib=mem * 0.9, cpu=8)
    snap = b.build(C)
    assert snap.budget_bytes == (mem - reserve_expect) * GiB
    big = (mem - reserve_expect) * 0.9
    b.queued("fits", mem=big)
    b2 = SnapBuilder(memsize_gib=mem, reclaimable_gib=mem * 0.9, cpu=8).queued("toobig", mem=mem - reserve_expect + 1)
    assert plan(b).admit == "fits"
    assert "memory_ledger" in plan(b2).blocked["toobig"]


def test_os_reserve_gib_override():
    snap = SnapBuilder(memsize_gib=48).build(cfg(os_reserve_gib=4))
    assert snap.budget_bytes == 44 * GiB


def test_dependency_waits_and_cascades_policy():
    b = SnapBuilder().dep("dep1", "queued").queued("B", after=["dep1"])
    snap = b.build(C)
    assert admit.eligible(snap.jobs[0], snap, C) == (False, "dependency")
    b = SnapBuilder().dep("dep1", "done").queued("B", after=["dep1"])
    assert plan(b).admit == "B"
    b = SnapBuilder().dep("dep1", "failed").queued("B", after=["dep1"])
    assert admit.eligible(b.build(C).jobs[0], b.build(C), C) == (False, "dependency_failed")


def test_pick_is_pure():
    b = SnapBuilder().running("A", mem=5).queued("B", mem=3).queued("C", mem=3, priority=3)
    snap = b.build(C)
    before = repr(snap)
    p1, p2 = admit.plan(snap, C), admit.plan(snap, C)
    assert p1 == p2 and repr(snap) == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.jobs[0].priority = 5


def test_usage_decays_halflife():
    assert decay(100.0, 21600, 21600) == pytest.approx(50.0)
    assert decay(100.0, 0, 21600) == 100.0


def test_usage_dt_clamped():
    b = SnapBuilder().running("A", gpu="exclusive", mem=4, started_ago=100)
    snap = b.build(C)
    out = charge({"p": 0.0}, {"p": snap.now_ms - 10**9}, snap.now_ms, snap, C)
    assert out["p"] == pytest.approx(3600.0)                # clamped to 1 h, share 1.0
    out = charge({"p": 0.0}, {"p": snap.now_ms + 10**6}, snap.now_ms, snap, C)
    assert out["p"] == 0.0                                  # clock went backwards: no negative dt


def test_share_is_dominant_resource():
    snap = SnapBuilder().build(C)
    j = SnapBuilder().queued("x", gpu="shared", mem=20, cpu_cores=2).build(C).jobs[0]
    assert share(j, snap) == pytest.approx(0.5)
    j = SnapBuilder().queued("x", gpu="none", mem=1, cpu_cores=9).build(C).jobs[0]
    assert share(j, snap) == pytest.approx(0.5)


def test_rank_key_order():
    snap = SnapBuilder().project("a", 100, 1).project("b", 100, 4).queued("ja", project="a").queued("jb", project="b") \
        .queued("hi", project="a", priority=5).build(C)
    order = sorted(snap.jobs, key=rank_key)
    assert [j.id for j in order] == ["hi", "jb", "ja"]


BLOCK_CASES = {
    "needs_estimate": lambda: SnapBuilder().queued("x", state="needs_estimate"),
    "dependency": lambda: SnapBuilder().dep("d", "queued").queued("x", after=["d"]),
    "dependency_failed": lambda: SnapBuilder().dep("d", "failed").queued("x", after=["d"]),
    "no_start_waiter": lambda: SnapBuilder().queued("x", spawn="external"),
    "window": lambda: SnapBuilder(now="2026-11-08T16:00:00Z").queued("x", queue="night", estimate_s=100),
    "fit_window": lambda: SnapBuilder(now="2026-11-08T23:00:00Z").queued("x", queue="night", estimate_s=10**5),
    "memory_ledger": lambda: SnapBuilder(reclaimable_gib=48).running("r", mem=35).queued("x", mem=10),
    "memory_live": lambda: SnapBuilder(reclaimable_gib=3).queued("x", mem=10),
    "gpu": lambda: SnapBuilder().running("r", gpu="exclusive").queued("x", gpu="exclusive"),
    "cpu": lambda: SnapBuilder(cpu=4).running("r", cpu_cores=4).queued("x", cpu_cores=1),
    "lock:L": lambda: SnapBuilder().running("r", locks=["L"]).queued("x", locks=["L"]),
    "protective": lambda: SnapBuilder(protective=True).queued("x", gpu="exclusive"),
    "pressure": lambda: SnapBuilder(pressure="critical").queued("x"),
    "swap_unknown": lambda: SnapBuilder(swap_known=False).queued("x"),
    "level": lambda: SnapBuilder(level=10).queued("x", level_min=50),
    "inhibited": lambda: SnapBuilder().queued("x", profile="pr", inhibited=True),
    "drain": lambda: SnapBuilder(draining=True).queued("x", profile="pr"),
    "project_missing": lambda: SnapBuilder().queued("x", project_present=False),
}


@pytest.mark.parametrize("code", sorted(BLOCK_CASES))
def test_blocked_by_for_each_code(code):
    b = BLOCK_CASES[code]()
    snap = b.build(C)
    e = explain_all(snap, C)["x"]
    assert code in e["blocked_by"], e
    assert e["why"] and "{" not in e["why"]


def test_blocked_by_reservation_code():
    b = SnapBuilder(reclaimable_gib=40).running("A", mem=30)
    b.queued("big", mem=20, priority=5, eligible_since_ms=b.now_ms - 5 * 3600 * 1000).queued("x", mem=3)
    assert explain_all(b.build(C), C)["x"]["blocked_by"] == ["reservation"]


def test_explain_position():
    b = SnapBuilder().queued("a", priority=1).queued("b", priority=0)
    e = explain_all(b.build(C), C)
    assert e["a"]["position"] == 1 and e["b"]["position"] == 2 and e["a"]["blocked_by"] == []
