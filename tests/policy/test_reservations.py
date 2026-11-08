"""Time reservations (docs/implementation/16 section 2). Pure policy tests."""

import datetime as dt

import pytest

from gsched.config import Config
from gsched.errors import ConfigError
from gsched.policy import admit, preempt, reservations, windows
from gsched.policy.fit import fits
from gsched.units import GiB
from tests.policy.builders import SnapBuilder, cfg, ms

R = {"name": "t233-pass", "days": ["mon", "tue", "wed", "thu", "fri"], "start": "19:00", "end": "23:30",
     "project": "latent-alpha", "memory_gib": 27, "gpu": "exclusive", "unload_restartable": True}
MON_20 = "2026-11-09T20:00:00Z"        # a Monday, inside the window
MON_17 = "2026-11-09T17:00:00Z"        # two hours before it opens


def c_res(**kw):
    kw.setdefault("night_window", None)
    kw.setdefault("reservations", (R,))
    return cfg(**kw)


def test_reservation_blocks_other_project_gpu_job_during_window():
    c = c_res()
    b = SnapBuilder(now=MON_20, reclaimable_gib=45)
    b.queued("other", gpu="exclusive", mem=4, project="p")
    b.queued("owner", gpu="exclusive", mem=4, project="latent-alpha")
    snap = b.build(c)
    jobs = {j.id: j for j in snap.jobs}
    ok, why = fits(jobs["other"], snap, c)
    assert not ok and why == ["reservation_window:t233-pass"]
    assert fits(jobs["owner"], snap, c) == (True, [])
    assert admit.plan(snap, c).admit == "owner"
    # without the reservation the same job fits
    assert fits(jobs["other"], b.build(cfg(night_window=None)), cfg(night_window=None))[0]


def test_reservation_allows_cpu_job_that_fits_remaining():
    c = c_res()
    b = SnapBuilder(now=MON_20, reclaimable_gib=45)           # budget 40 GiB, 27 reserved -> 13 left for others
    b.queued("small", gpu="none", mem=10, project="p")
    b.queued("big", gpu="none", mem=14, project="p")
    snap = b.build(c)
    jobs = {j.id: j for j in snap.jobs}
    assert fits(jobs["small"], snap, c)[0]
    ok, why = fits(jobs["big"], snap, c)
    assert not ok and why == ["reservation_window:t233-pass"]
    # what the owner already holds counts against the reservation, so more is left for others
    b2 = SnapBuilder(now=MON_20, reclaimable_gib=45)
    b2.running("held", project="latent-alpha", mem=20, gpu="none")
    b2.queued("fits", gpu="none", mem=13, project="p")
    b2.queued("over", gpu="none", mem=14, project="p")
    s2 = b2.build(c)
    assert reservations.reserve_remaining(reservations.parse(c)[0], s2) == 7 * GiB
    assert fits(s2.jobs[-2], s2, c)[0]                      # 20 held + 13 == 40 - 7
    assert fits(s2.jobs[-1], s2, c)[1] == ["reservation_window:t233-pass"]


def test_reservation_overlap_rule_blocks_long_job_before_window():
    c = c_res()
    b = SnapBuilder(now=MON_17, reclaimable_gib=45)
    b.queued("long", gpu="none", mem=20, project="p", estimate_s=3 * 3600)          # would run until 20:00
    b.queued("unknown", gpu="none", mem=20, project="p", estimate_s=None, checkpoint="atomic")
    snap = b.build(c)
    jobs = {j.id: j for j in snap.jobs}
    assert fits(jobs["long"], snap, c)[1] == ["reservation_window:t233-pass"]
    # no estimate: default_unknown_estimate_s (7200) is used -> 19:00 is not overlapped, 17:00 + 2h == 19:00
    assert fits(jobs["unknown"], snap, c)[0]
    c2 = c_res(default_unknown_estimate_s=7201)
    assert fits(jobs["unknown"], snap, c2)[1] == ["reservation_window:t233-pass"]


def test_reservation_short_job_before_window_runs():
    c = c_res()
    b = SnapBuilder(now=MON_17, reclaimable_gib=45)
    b.queued("short", gpu="exclusive", mem=20, project="p", estimate_s=3600)         # ends 18:00
    b.queued("restartable", gpu="exclusive", mem=20, project="p", checkpoint="restartable", estimate_s=None)
    snap = b.build(c)
    assert fits(snap.jobs[0], snap, c)[0]
    # restartable without an estimate is exempt from the overlap rule (it can be unloaded when the window opens)
    assert fits(snap.jobs[1], snap, c)[0]


def test_reservation_owner_project_unaffected_and_day_queue_treatment():
    c = c_res(night_window={"start": "23:00", "end": "08:00"})
    b = SnapBuilder(now=MON_20, reclaimable_gib=45)
    b.queued("owner-night", gpu="exclusive", mem=20, project="latent-alpha", queue="night", checkpoint="atomic",
             estimate_s=4 * 3600)
    b.queued("other-night", gpu="none", mem=2, project="p", queue="night")
    snap = b.build(c)
    jobs = {j.id: j for j in snap.jobs}
    # 20:00 is outside the night window: a night job normally waits, but the owner's window lifts that restriction
    assert admit.eligible(jobs["owner-night"], snap, c) == (True, None)
    assert admit.eligible(jobs["other-night"], snap, c) == (False, "window")
    assert fits(jobs["owner-night"], snap, c)[0]
    # outside the owner's window the queue rule applies again
    late = SnapBuilder(now="2026-11-09T18:00:00Z", reclaimable_gib=45)
    late.queued("owner-night", gpu="exclusive", mem=20, project="latent-alpha", queue="night", estimate_s=60)
    s2 = late.build(c)
    assert admit.eligible(s2.jobs[0], s2, c) == (False, "window")


def test_reservation_wraps_midnight_days_rule():
    wrap = dict(R, name="late", days=["fri"], start="22:00", end="02:00")
    c = c_res(reservations=(wrap,))
    r = reservations.parse(c)[0]
    sat_01 = ms("2026-11-14T01:00:00Z")
    assert reservations.current(r, sat_01, c) is not None          # started Friday 22:00
    assert reservations.current(r, sat_01, c).date == "2026-11-13"
    assert reservations.current(r, ms("2026-11-14T02:00:00Z"), c) is None     # end is exclusive
    assert reservations.current(r, ms("2026-11-14T22:30:00Z"), c) is None     # Saturday is not a start day
    assert reservations.current(r, ms("2026-11-13T01:00:00Z"), c) is None     # Friday before it starts
    assert reservations.current(r, ms("2026-11-13T23:59:00Z"), c) is not None
    daily = c_res(reservations=(dict(wrap, days=["daily"]),))
    assert reservations.current(reservations.parse(daily)[0], ms("2026-11-15T00:30:00Z"), daily) is not None
    # end <= start with equal times is a 24 h window
    full = c_res(reservations=(dict(wrap, start="08:00", end="08:00", days=["daily"]),))
    assert reservations.current(reservations.parse(full)[0], ms("2026-11-10T07:59:00Z"), full) is not None


def test_reservation_dst():
    c = c_res(timezone="America/Los_Angeles",
              reservations=(dict(R, name="dst", days=["daily"], start="01:30", end="03:30", gpu="none"),))
    r = reservations.parse(c)[0]
    # spring forward, 2026-03-08: 01:30 PST exists, 03:30 is PDT; the window is only one real hour long
    occ = [o for o in reservations.occurrences(r, ms("2026-03-08T12:00:00Z"), c) if o.date == "2026-03-08"][0]
    assert (occ.end_ms - occ.start_ms) == 3600 * 1000
    assert dt.datetime.fromtimestamp(occ.start_ms / 1000, tz=dt.timezone.utc).hour == 9       # 01:30 PST == 09:30Z
    # fall back, 2026-11-01: 01:30 happens twice; the window spans the repeated hour
    occ2 = [o for o in reservations.occurrences(r, ms("2026-11-01T12:00:00Z"), c) if o.date == "2026-11-01"][0]
    assert 2 * 3600 * 1000 <= occ2.end_ms - occ2.start_ms <= 3 * 3600 * 1000
    assert reservations.current(r, occ.start_ms + 60_000, c) is not None


def test_reservation_upcoming_and_active():
    c = c_res()
    assert [r.name for r in reservations.active(ms(MON_20), c)] == ["t233-pass"]
    assert reservations.active(ms(MON_17), c) == []
    up = reservations.upcoming(ms(MON_17), c, 3 * 3600 * 1000)
    assert [(r.name, o.start_ms) for r, o in up] == [("t233-pass", ms("2026-11-09T19:00:00Z"))]
    assert reservations.upcoming(ms(MON_17), c, 3600 * 1000) == []


def test_reservation_gpu_modes():
    shared = c_res(reservations=(dict(R, gpu="shared"),))
    b = SnapBuilder(now=MON_20, reclaimable_gib=45)
    b.queued("excl", gpu="exclusive", mem=2, project="p")
    b.queued("shr", gpu="shared", mem=2, project="p")
    snap = b.build(shared)
    jobs = {j.id: j for j in snap.jobs}
    assert fits(jobs["excl"], snap, shared)[1] == ["reservation_window:t233-pass"]
    assert fits(jobs["shr"], snap, shared)[0]                                      # one of two slots stays free
    b2 = SnapBuilder(now=MON_20, reclaimable_gib=45)
    b2.running("s1", gpu="shared", mem=2, project="p")
    b2.queued("shr", gpu="shared", mem=2, project="p")
    s2 = b2.build(shared)
    assert fits(s2.jobs[-1], s2, shared)[1] == ["reservation_window:t233-pass"]    # 1 held + 1 reserved == max 2
    # an owner job already holding the exclusive GPU: normal GPU rules block, the reservation adds nothing
    b3 = SnapBuilder(now=MON_20, reclaimable_gib=45)
    b3.running("o", gpu="exclusive", mem=10, project="latent-alpha")
    b3.queued("x", gpu="exclusive", mem=2, project="p")
    s3 = b3.build(c_res())
    assert fits(s3.jobs[-1], s3, c_res())[1] == ["gpu"]


def _start_snap(now=MON_20, **kw):
    b = SnapBuilder(now=now, reclaimable_gib=45, **kw)
    return b


def test_reservation_start_plan_unloads_restartable_and_checkpoints_cooperative():
    c = c_res()
    b = _start_snap()
    b.running("rs", project="p", gpu="exclusive", mem=20, checkpoint="restartable", priority=50, started_ago=5)
    snap = b.build(c)
    p = reservations.plan(snap, c)
    # priority and restartable_min_hold do not protect it; the owner's window opens now
    assert p.stop == [("rs", "reservation")] and p.checkpoint == [] and p.freeze_admission
    assert p.set_meta == {"reservation_latch:t233-pass": "2026-11-09"}
    b2 = _start_snap()
    b2.running("co", project="p", gpu="exclusive", mem=20, checkpoint="cooperative")
    p2 = reservations.plan(b2.build(c), c)
    assert p2.checkpoint == [("co", "reservation")] and p2.stop == []
    # latched: nothing more today
    b3 = _start_snap()
    b3.running("rs", project="p", gpu="exclusive", mem=20, checkpoint="restartable")
    b3.reservation_latches = {"t233-pass": "2026-11-09"}
    assert reservations.plan(b3.build(c), c).is_empty()
    # unload_restartable=false: the latch is set, nobody is touched
    off = c_res(reservations=(dict(R, unload_restartable=False),))
    p4 = reservations.plan(b.build(off), off)
    assert p4.stop == [] and p4.set_meta == {"reservation_latch:t233-pass": "2026-11-09"}


def test_reservation_never_signals_atomic():
    c = c_res()
    b = _start_snap()
    b.running("at", project="p", gpu="exclusive", mem=20, checkpoint="atomic")
    b.running("sp", project="p", gpu="none", mem=5, checkpoint="cooperative", held_reason="spilled", spilled=True)
    b.running("ex", project="p", gpu="none", mem=1, checkpoint="restartable", spawn="external")
    p = reservations.plan(b.build(c), c)
    assert p.stop == [] and p.checkpoint == [] and p.kill == [] and p.kill_stop == []
    kinds = [e[0] for e in p.events]
    assert "reservation_blocked_by_atomic" in kinds
    ev = [e for e in p.events if e[0] == "reservation_blocked_by_atomic"][0]
    assert "at" in ev[2]["jobs"]
    # the owner's own jobs are never victims
    b2 = _start_snap()
    b2.running("own", project="latent-alpha", gpu="exclusive", mem=20, checkpoint="restartable")
    assert reservations.plan(b2.build(c), c).stop == []


def test_reservation_validation():
    base = {"name": "a", "days": ["mon"], "start": "19:00", "end": "23:30", "project": "p", "memory_gib": 1}
    assert Config.from_dict({"reservations": [base]}).reservations[0]["name"] == "a"
    bad = [dict(base, name="Bad_Name"), dict(base, name="x" * 41), dict(base, days=["Mon"]), dict(base, days=[]),
           dict(base, days="mon"), dict(base, start="7pm"), dict(base, end="24:00"), dict(base, memory_gib=0),
           dict(base, memory_gib=True), dict(base, gpu="all"), dict(base, unload_restartable="yes"),
           dict(base, extra=1), {k: v for k, v in base.items() if k != "project"}]
    for b in bad:
        with pytest.raises(ConfigError):
            Config.from_dict({"reservations": [b]})
    with pytest.raises(ConfigError, match="duplicate"):
        Config.from_dict({"reservations": [base, dict(base)]})
    with pytest.raises(ConfigError):
        Config.from_dict({"reservations": "nope"})
    for k, v in (("default_unknown_estimate_s", 0), ("job_log_max_bytes", 10), ("job_log_keep_bytes", -1),
                 ("tcc_protected_roots", ["relative/dir"])):
        with pytest.raises(ConfigError):
            Config.from_dict({k: v})
    with pytest.raises(ConfigError):
        Config.from_dict({"job_log_max_bytes": 2048, "job_log_keep_bytes": 4096})
    assert Config.from_dict({"job_log_max_bytes": 4096, "job_log_keep_bytes": 1024}).job_log_keep_bytes == 1024


def test_reservation_victims_stay_deterministic():
    """The window mode planner is pure: same snapshot, same answer."""
    c = c_res()
    b = _start_snap()
    b.running("a", project="p", gpu="exclusive", mem=20, checkpoint="restartable")
    snap = b.build(c)
    assert reservations.plan(snap, c) == reservations.plan(snap, c)
    t = reservations._virtual_target(reservations.parse(c)[0], snap)
    assert preempt.find_victims(t, snap, c, "window") == ["a"]
